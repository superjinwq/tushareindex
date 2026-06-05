# 成交额 Top5%_pct 弹窗 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 点击成交额表头 `Top5%_pct` 打开与超跌同款弹窗，左轴 `amount_top5_pct` + 五年 2σ 黄虚线，右轴 399317.SZ 收盘，含当日实时拼接。

**Architecture:** 克隆 `/api/up_num_ma5_series` 与超跌 modal 全套（API 缓存、rt 拼接、Chart.js zoom、drag/resize/localStorage）；2σ 边界在后端按五年 `marketinfo.amount_top5_pct` 算 mean±2σ 随 JSON 返回。

**Tech Stack:** Flask (`rt_monitor_app.py`)、Chart.js + chartjs-plugin-zoom、原生 JS/CSS、MySQL `stock_tushare` / `stock_wind`

**设计文档：** `docs/plans/2026-06-05-amount-top5-pct-modal-design.md`

---

### Task 1: 后端 2σ 边界辅助函数

**Files:**
- Modify: `rt_monitor_app.py`（`_read_amount_top5_pct_percentile` 附近）

**Step 1:** 新增 `_compute_amount_top5_pct_sigma2_bounds(conn_tushare, conn_wind) -> tuple[float|None, float|None]`

- 复用 `_read_amount_top5_pct_percentile` 的锚定日与五年 `lo` 日期
- 读取 `amount_top5_pct` 列表（不含 anchor 当日）
- `mean`、`pstdev`；若样本 < 2 或 std=0 返回 `(None, None)`
- 返回 `(mean - 2*std, mean + 2*std)`，各保留 2 位小数

**Step 2:** 本地快速验证

```bash
python -c "
# 调用新函数或内联同等 SQL，确认 sigma2_low/high 与 design 试算接近
"
```

**Step 3:** Commit

```bash
git add rt_monitor_app.py
git commit -m "feat(rt-monitor): add amount_top5_pct 5y sigma2 bounds helper"
```

---

### Task 2: 后端 `/api/amount_top5_pct_series`

**Files:**
- Modify: `rt_monitor_app.py`（`get_up_num_ma5_series` 之后仿写）

**Step 1:** 新增路由 `GET /api/amount_top5_pct_series`

```python
@app.route('/api/amount_top5_pct_series')
def get_amount_top5_pct_series():
    # limit 默认 300，最大 1300
    # SQL: marketinfo m INNER JOIN index_daily i ON trade_date, ts_code='399317.SZ'
    # 字段: m.trade_date, m.amount_top5_pct, i.close
    # 当日拼接: _compute_stockstat_amount_top5_pct + rt_idx_k
    # 响应含 sigma2_low, sigma2_high
    # _popup_series_cache_get/set
```

**Step 2:** 将新路径加入 popup 免缓存白名单（若 `after_request` 有 API 列表，与 `up_num_ma5_series` 并列）

**Step 3:** curl 验证

```bash
curl -s "http://localhost:5000/api/amount_top5_pct_series?limit=5" | python -m json.tool
```

Expected: `times`、`amount_top5_pct`、`close` 数组等长；`sigma2_low`/`sigma2_high` 为浮点数。

**Step 4:** Commit

```bash
git add rt_monitor_app.py
git commit -m "feat(rt-monitor): add /api/amount_top5_pct_series with rt splice and 2σ bounds"
```

---

### Task 3: HTML modal 骨架

**Files:**
- Modify: `templates/rt_monitor.html`（超跌 modal 之后）

**Step 1:** 插入 overlay 块，id 前缀 `amount-top5-pct-modal-*`，标题：

`成交额Top5%占比与399317.SZ收盘价（最近约5年交易日）`

结构照抄 `#up-num-ma5-modal-overlay`（header / close / loading / msg / canvas）。

**Step 2:** Commit

```bash
git add templates/rt_monitor.html
git commit -m "feat(rt-monitor): add amount_top5_pct modal HTML shell"
```

---

### Task 4: CSS

**Files:**
- Modify: `static/css/rt_monitor.css`

**Step 1:** 复制 `.up-num-ma5-modal-shell` / `.up-num-ma5-modal-chart-wrap` 为 `.amount-top5-pct-modal-shell` / `.amount-top5-pct-modal-chart-wrap`

**Step 2:** 表头可点击样式

```css
.amount-top5-header-metric.amount-top5-pct-clickable {
    cursor: pointer;
    text-decoration: underline;
    text-decoration-style: dotted;
}
.amount-top5-header-metric.amount-top5-pct-clickable:hover {
    opacity: 0.85;
}
```

**Step 3:** 文件末尾更新时间戳注释

**Step 4:** Commit

```bash
git add static/css/rt_monitor.css
git commit -m "style(rt-monitor): amount_top5_pct modal shell and clickable header"
```

---

### Task 5: JS — 弹窗生命周期与图表

**Files:**
- Modify: `static/js/rt_monitor.js`

**Step 1:** 常量

```javascript
var AMOUNT_TOP5_PCT_MODAL_SERIES_LIMIT = 1300;
var AMOUNT_TOP5_PCT_MODAL_LAYOUT_STORAGE_KEY = 'rt_monitor_amount_top5_pct_modal_layout_v1';
```

**Step 2:** 复制并改名：`save/apply/scheduleSaveAmountTop5PctModalLayout`、`closeAmountTop5PctModal`、`openAmountTop5PctExpandModal`、`initAmountTop5PctModal`、`renderAmountTop5PctModalChart`

`renderAmountTop5PctModalChart` 要点：

- 左轴 `yPct`：`amount_top5_pct`（红 `#dc2626`）
- 右轴 `yClose`：399317.SZ 收盘（紫，与超跌一致）
- 2σ 线：`payload.sigma2_low` / `sigma2_high`，样式同 BIAS `line2sigmaLow/High`，`yAxisID: 'yPct'`
- zoom/pan 与超跌相同（category X、limits 0..n-1、初始 300 窗）
- tooltip 跳过 reference line dataset

**Step 3:** `setAmountTop5HeaderMetrics` 内给 `#amount-top5-pct-header` 加 class `amount-top5-pct-clickable`

**Step 4:** `initAmountTop5PctModal()` 在 DOMContentLoaded 与 `initUpNumMa5Modal` 并列调用

**Step 5:** 点击 `#amount-top5-pct-header` → `openAmountTop5PctExpandModal()`

**Step 6:** Esc 关闭栈、`_modalOpened/_modalClosed`、popup fetch abort key `amountTop5Pct` 纳入现有逻辑（搜 `up-num-ma5-modal-overlay` 的 Esc 分支照抄）

**Step 7:** Commit

```bash
git add static/js/rt_monitor.js
git commit -m "feat(rt-monitor): amount_top5_pct modal chart with 2σ lines and click handler"
```

---

### Task 6: 静态版本号与文件时间戳

**Files:**
- Modify: `rt_monitor_app.py`（`RT_MONITOR_STATIC_VER` 递增，如 `20260605-1`）
- Modify: `rt_monitor_app.py` 末尾 `# 代码更新时间：...`
- Modify: `static/js/rt_monitor.js`、`static/css/rt_monitor.css` 末尾时间戳

**Step 1:**  bump `RT_MONITOR_STATIC_VER`

**Step 2:** Commit

```bash
git add rt_monitor_app.py static/js/rt_monitor.js static/css/rt_monitor.css
git commit -m "chore(rt-monitor): bump static ver for amount_top5_pct modal"
```

---

### Task 7: 手工验收

**前提:** 单实例 `rt_monitor_app.py`（见 `docs/solutions/workflow-issues/rt-monitor-multi-instance-debug-checklist-20260426.md`）

**Checklist:**

1. 打开 `http://localhost:5000/`，成交额表头 `Top5%_pct` 有 pointer/hover
2. 单击弹出窗口，可拖标题栏、右下角 resize
3. 左轴红线 + 上下黄色 2σ 虚线；右轴紫线收盘
4. 滚轮缩放、横向拖动浏览全量
5. 关闭重开，窗口位置/大小保持
6. 弹窗打开期间 Network 无多余 dashboard 轮询（或轮询暂停）
7. 响应头 `X-RT-Monitor-Build` 与新 `RT_MONITOR_STATIC_VER` 一致

**Step 2:** 记录验收结果于 commit message 或 PR 描述（无需新文件）

---

## 执行选项

Plan 已保存至 `docs/plans/2026-06-05-amount-top5-pct-modal.md`。

1. **本会话 Subagent-Driven** — 按 Task 逐条实现并复核  
2. **新会话 executing-plans** — 独立会话批量执行  

请选择执行方式。
