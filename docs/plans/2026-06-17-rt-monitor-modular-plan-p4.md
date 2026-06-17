# P4 — 财报筛选域（fina_bp）迁移

> 2026-06-17 执行；**P4.1** 业务下沉、**P4.2** 查询迁出、**top10 优化** 同日收尾。

## 交付（P4 路由层）

| 项 | 路径 | 说明 |
|----|------|------|
| Blueprint | `rt_monitor/blueprints/fina_bp.py` | 页面 + 3 条 API |
| 剥离 | `scripts/p4_strip_fina_routes.py` | 去掉 4 条 `@app.route` |
| 验收 | `scripts/p4_verify.py` | fina 专验；`p3_verify all` 回归 |

### 路由清单

| URL | 处理 |
|-----|------|
| `/rt-monitor/fina` | `fina_bp` → `fina_arturn.renderFinaPageNoCache` |
| `/api/fina_arturn_days_table` | `queryFinaArturnDaysTable` |
| `/api/fina_fixed_assets_revenue_series` | `queryFinaFixedAssetsRevenueSeries` |
| `/api/fina_stock_basic_act` | `queryFinaStockBasicAct` |

首页导航：`url_for('fina.rt_monitor_fina')`（与 P1–P3 子页一致）。

## 验收（2026-06-17）

- [x] `python scripts/p4_verify.py` — **PASS**（arturn rows≈5338–5416）
- [x] `python scripts/p3_verify.py all` — **PASS**
- [x] 8 个 Blueprint：`main` … `niusan` + **`fina`**
- [x] 本机重启预热：fast_path 正常，无「快速路径失败」
- [x] Commit：`refactor(rt_monitor): P4 fina 域迁移与 top10 优化`

## P4.1（2026-06-17）

- [x] `rt_monitor/services/fina_arturn.py`：页面渲染、三条 API 业务、启动预热
- [x] `fina_bp` 仅 HTTP 层
- [x] `app_factory` 预热改 `warmupFinaArturnCacheAsync`
- [x] `scripts/p4_strip_fina_handlers.py` 去掉 monolith 重复 handler

## P4.2（2026-06-17，已完成）

- [x] `rt_monitor/services/fina_arturn_queries.py`：`_query_fina_arturn_table` 及 attach 链（~5300 行）
- [x] `scripts/p4_extract_fina_queries.py` 抽取脚本；monolith 保留 re-export + `_sw_*` 供行业统计
- [x] `rt_monitor_app.py`：`_fina_*` 改从 `fina_arturn_queries` 导入
- [x] 迁出后补缺：`concurrent.futures`、`statistics`、`numpy`、`_tushare_column_exists` 等

## P4.3 top10_holders 性能（2026-06-17）

- [x] 方案与实测：`docs/plans/2026-06-17-fina-top10-holders-perf.md`
- [x] 单次 IN 全量查询替代 chunk=450 串行（top3 阶段 ~15s → ~3s）

## 残留（设计目标态，非本里程碑）

- `monolith.py` 仍承载 limit_stats / ETF / 申万·指数行业统计等（main_bp `_mono()` 委托）
- `core/cache.py`、`services/sw_industry.py` 等未建 — 按需再拆

*计划版本：2026-06-17 19:22*
