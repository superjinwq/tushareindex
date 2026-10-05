# rt_monitor/monolith.py 拆分路线图（2026-10-04）

## 现状
- P1–P4（历史）：路由迁到 blueprints，财报查询迁到 `services/fina_arturn_queries.py`。
- **P5.1（本次，已完成）**：成交额历史极值/均值 12 个只读查询函数（400 行）迁到 `rt_monitor/services/amount_extremes.py`，monolith 原位置 re-export 原名。
  - 工具：`scripts/p5_extract_amount_extremes.py`（按 AST 定位区块；拒绝覆盖已有文件、拒绝区块内含非函数语句、拒绝 CRLF/LF 混用；按字节保留 CRLF）。
  - 验证：12 个函数源码逐字一致；函数数守恒（135 + 12 = 147）；`url_map` 58 条哈希不变（`0e23f59876270881`）；`/api/rt_median`、`/api/idx_minute_diff_prev5?ts_code=399317.SZ` 归一化响应哈希不变；19 个页面/接口全部 200，行数一致；pytest 861 项全部通过。
- monolith 现为 **7154 行、136 个顶层定义、0 条路由**。

## 后续每步的固定做法（与 P5.1 相同）
1. 采集基线：`url_map` 哈希、相关接口的归一化响应哈希、行数。
2. 复制一份 `scripts/p5_extract_*.py`，只改「起止函数名 / 目标模块 / 文件头 import」三处。
3. 检查迁出块是否引用 monolith 的全局可变状态（锁、缓存字典、executor）。**有就整组一起迁**，或先把状态迁到 `core/` 再迁函数，不能拆开。
4. 检查测试里的 `monkeypatch.setattr(monolith, ...)`：迁出后，块内部的互相调用会解析到新模块，patch monolith 上的名字将**不再生效**，需要把 patch 目标改到新模块。
5. 改完：py_compile → 源码逐字比对 → pytest → bat 重启并确认单实例 → 复测基线哈希。

## 建议顺序（按风险从低到高）

| 步 | 区块（迁出后行号） | 目标模块 | 规模 | 风险点 |
|---|---|---|---|---|
| P5.2 | `_ensure_etf_metric_history` / `_attach_etf_ma_metric_percentiles` / `_safe_attach_…`（2719–2776） | `services/etf_ma_metrics.py` | 约 60 行 | 低；只在 monolith 内部调用 |
| P5.3 | `read_index_stat`、`_fmt_date`（2821–2899） | `services/index_stat.py` | 约 80 行 | 外部脚本 `盘中实时统计.py` 引用了 `read_index_stat`，必须 re-export |
| P5.4 | `read_limit_stats_panel` / `read_limit_stats_detail`（3075–3633） | `services/limit_stats.py` | 约 560 行 | 中；先确认依赖的辅助函数 `limit*`（551–1202）是否要一起迁 |
| P5.5 | ma_signal 刷新组：`_build_ma_signal_fallback_rows` 到 `_warmup_ma_signal_table_cache_async`（2952–3072） | `services/ma_signal.py` | 约 120 行 | **高**：持有进程池 executor 与异步刷新状态；`app_factory` 和 `tests/test_rt_monitor_realtime_freshness.py` 直接引用，需整组迁 |
| P5.6 | top_moves 后台循环（`fetchTopMovesOnce` / `topMovesRefreshLoop`，约 4260–4352） | `services/top_moves.py` | 约 100 行 | **高**：后台线程与共享缓存，需要盘中验证 |
| P5.7 | `DataFetcher` 类（2377–2492）、`load_initial_cache`（2902–2949） | `core/data_fetcher.py` | 约 170 行 | **高**：启动路径，`app_factory` 引用；放到最后 |

**建议每次只做一步，并在盘后进行**（P5.5、P5.6 涉及后台线程，盘中重启会打断实时刷新）。
