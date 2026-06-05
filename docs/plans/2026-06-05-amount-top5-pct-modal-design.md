# 成交额 Top5%_pct 弹窗设计说明

**日期**：2026-06-05  
**状态**：已确认（用户批准，含 2σ 参考线补充）  
**关联**：`rt_monitor_app.py`、`static/js/rt_monitor.js`、`templates/rt_monitor.html`

## 目标

在行情监控页「成交额（亿）」表头，点击 `Top5%_pct：xx.xx%` 打开弹窗，双轴展示：

- **左轴**：`marketinfo.amount_top5_pct`（%）
- **右轴**：`index_daily` 中 `399317.SZ`（国证 A 指）`close`
- **X 轴**：`trade_date`

弹窗交互与「涨跌家数 → 超跌」弹窗一致：可拖动、可 resize、`localStorage` 记几何、Chart.js 横向 pan/zoom、打开时暂停主面板轮询。

## 已锁定口径

| 项 | 约定 |
|----|------|
| 点击入口 | 仅 `#amount-top5-pct-header`；`Per` 不可点击 |
| 指数代码 | `399317.SZ`（本库有数据；`.SH` 无数据） |
| 历史序列 | `marketinfo` INNER JOIN `index_daily`，按 `trade_date` 对齐 |
| 当日拼接 | 与超跌一致：锚定日 = `rt_k` 最大 `trade_time` 的 DATE；`amount_top5_pct` 用 `_compute_stockstat_amount_top5_pct`；收盘用 `rt_idx_k`（399317.SZ）；末点覆盖或追加 |
| 序列长度 | `limit` 默认 300，最大 1300（约 5 年） |
| 初始视窗 | 最近 300 个交易日 |

## 2σ 参考线（用户补充）

视觉与 **BIAS Per 表 → 日线 10MA 弹窗** 一致：

- 黄色虚线 `rgba(255, 193, 7, 0.9)`，`borderDash: [4, 2]`
- 绑定左轴 `yPct`（`amount_top5_pct`）
- 水平贯穿全图（`Array(n).fill(value)`）

**数值口径**（与表头 `Per` 五年窗口对齐，但用原始 % 而非经验分位）：

- 样本：`marketinfo.amount_top5_pct`，窗口 = `AMOUNT_TOP5_PCTILE_LOOKBACK_YEARS`（5 年），`trade_date >= anchor-5y` 且 `trade_date < anchor`（不含锚定当日，与 `_read_amount_top5_pct_percentile` 一致）
- 计算：`mean`、`population std`（`statistics.pstdev` 或等价）
- `sigma2_low = mean - 2 * std`
- `sigma2_high = mean + 2 * std`
- API 返回标量 `sigma2_low`、`sigma2_high`；前端画两条参考线，tooltip 中参考线项可隐藏（与 BIAS 弹窗一致）

本机试算（2026-06-04 锚定，n=1231）：mean≈40.83，std≈3.50，2σ 下≈33.83，上≈47.82。

## 方案

**方案 1（采用）**：克隆超跌弹窗独立实现 — 新 API + 新 modal DOM/JS/CSS，风险最小。

| 方案 | 优点 | 缺点 |
|------|------|------|
| 1 独立克隆 | 与现网一致、无回归 | ~150 行 JS 重复 |
| 2 复用同一壳 | 少一份 HTML | 改超跌逻辑，回归风险大 |
| 3 抽公共库 | 长期 DRY | 本次过重 |

## 后端 API

`GET /api/amount_top5_pct_series?limit=300`

响应：

```json
{
  "times": ["2024-01-02T00:00:00", "..."],
  "amount_top5_pct": [48.12, "..."],
  "close": [5123.4, "..."],
  "sigma2_low": 33.83,
  "sigma2_high": 47.82
}
```

- 缓存：`_popup_series_cache_get/set`，key `('amount_top5_pct_series', limit)`
- 2σ 边界单独函数 `_compute_amount_top5_pct_sigma2_bounds(conn_tushare, conn_wind)`，复用五年窗口与锚定日逻辑

## 前端

- Overlay：`#amount-top5-pct-modal-overlay`
- Shell：`amount-top5-pct-modal-shell`（复制 `.up-num-ma5-modal-shell` 样式）
- `localStorage`：`rt_monitor_amount_top5_pct_modal_layout_v1`
- `fetchPopupJson` key：`amountTop5Pct`
- 表头加 `cursor:pointer`、hover 提示、`bias-cell-clickable` 同类样式

图表 datasets 顺序建议：右轴收盘线 → 左轴 `amount_top5_pct` → `2σ下` → `2σ上`。

## 涉及文件

| 文件 | 变更 |
|------|------|
| `rt_monitor_app.py` | 路由 + 拼接 + 2σ 计算 |
| `templates/rt_monitor.html` | modal DOM |
| `static/css/rt_monitor.css` | shell/chart-wrap + 表头可点击样式 |
| `static/js/rt_monitor.js` | open/close/render/init + click 绑定 |
| `RT_MONITOR_STATIC_VER` | 递增 |

## 验收标准

1. 点击 `Top5%_pct` 弹出可拖动、可 resize 窗口  
2. 左轴 `amount_top5_pct`、右轴 399317.SZ 收盘，X 为交易日  
3. 左轴有两条黄色 2σ 虚线，数值来自近 5 年 `amount_top5_pct` 的 mean±2σ  
4. 末点与表头实时值、指数盘中价一致（交易日盘中）  
5. 横向 pan/zoom 正常；几何 localStorage 持久化  
6. 弹窗打开时主面板轮询暂停  

## 非目标

- `Per` 表头不可点击  
- 不抽公共 modal 框架（本次）  
- 不加 3σ 红线（除非后续单独需求）
