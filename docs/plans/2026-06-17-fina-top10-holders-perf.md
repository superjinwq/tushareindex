# fina arturn — top10_holders top3 性能优化

**日期**：2026-06-17  
**范围**：`rt_monitor/services/fina_arturn_queries.py` 中 `_fina_arturn_attach_top10_holders_top3`  
**背景**：P4.2 迁出后预热日志显示 `top10_holders_top3≈15s`，占整表 wall 约 55%。

---

## 1. 现象

启动预热（冷缓存）典型分段（`end=20260331`，rows≈5416）：

| 阶段 | 优化前耗时 |
|------|-----------|
| top10_holders_top3 | **~14.9s** |
| fast_path | ~1.8s |
| 其余 attach 合计 | ~8s |
| **wall** | **~25s** |

WARNING `fina_arturn 慢查询 wall=…` 为 **≥4s 的性能观测**（非错误）；快速路径正常时日志含 `fast_path=…s`。

---

## 2. 根因

原实现将约 **5416** 个 `ts_code` 按 **chunk_size=450** 串行执行 12 次相同 CTE：

```sql
WITH latest AS (SELECT ts_code, MAX(end_date) … WHERE ts_code IN (…)),
     ranked AS (ROW_NUMBER … PARTITION BY ts_code)
SELECT … WHERE rk <= 3
```

瓶颈在 **往返次数 × 单次扫描**，而非 SQL 口径错误。

---

## 3. 本机实测（2026-06-17）

同一 `codes` 列表、同一 SQL 口径：

| 策略 | 耗时 | 结果行数 |
|------|------|----------|
| chunk=450 串行（原） | **13.9s** | 16209 |
| chunk=2000 串行 | 4.9s | 16209 |
| **单次 IN 全量** | **2.7s** | 16209 |

结论：全市场规模下单次查询最优；保留分块 + 并行仅作超大列表兜底。

---

## 4. 实施方案（已落地）

1. 抽出 `_TOP10_HOLDERS_TOP3_SQL`、`_fetch_top10_holders_top3_map`。
2. **默认**：`len(codes) ≤ FINA_TOP10_HOLDERS_SINGLE_QUERY_MAX`（默认 **8000**）时 **一次** `IN` 查询。
3. **兜底**：超过阈值时 `chunk=2000` + `ThreadPoolExecutor`（workers 默认 4，cap 4）。
4. 环境变量（可选）：
   - `FINA_TOP10_HOLDERS_SINGLE_QUERY_MAX`
   - `FINA_TOP10_HOLDERS_CHUNK_SIZE`
   - `FINA_TOP10_HOLDERS_PARALLEL_WORKERS`

**不改**：业务口径（最新 `end_date`、按 `hold_ratio` 取 top3）、返回字段。

---

## 5. 预期收益

| 指标 | 优化前 | 优化后（本机） |
|------|--------|----------------|
| top10_holders_top3 | ~15s | **~2.7s** |
| 整表 wall（冷） | ~25s | **~13s** 量级 |

预热 3 期合计由 ~70s 降至 ~40s 量级（仍会有 `慢查询` WARNING，因 wall>4s）。

---

## 6. 可选后续（未做）

- 索引建议：`(ts_code, end_date DESC)` 或覆盖 `hold_ratio` 的复合索引（需结合表体量评估）。
- 进程内按「报告期 + 代码集合 hash」短 TTL 缓存 top3 结果（与 arturn 整包缓存重叠，收益有限）。
- 将 `慢查询` 阈值在预热线程改为 INFO，减少日志噪音。

---

## 7. 验收

```bash
python scripts/p4_verify.py
python scripts/p3_verify.py all
```

重启 `run_rt_monitor.bat` 后，预热日志中 `top10_holders_top3` 应降至 **3s 内**（本机）。

*文档版本：2026-06-17 19:22*
