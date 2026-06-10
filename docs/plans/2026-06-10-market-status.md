# 市场状态页 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在行情监控侧栏新增「市场状态」链接，独立页展示 marketinfo 七列历史表，顶栏切换 300/1300/全量，三列表头打开可拖可缩折线弹窗。

**Architecture:** 仿 `rt_monitor_margin` / `rt_monitor_fina` 独立子页；后端 `GET /api/marketinfo_recent` 只读 marketinfo；前端 `rt_monitor_market_status.js` 渲染表格与单壳 Chart 弹窗，复用 `bias-series-modal` CSS 与超跌拖动/resize 逻辑。

**Tech Stack:** Flask (`rt_monitor_app.py`)、Chart.js（`chart-stack.umd.js`）、原生 JS/CSS、MySQL `stock_tushare.marketinfo`

**设计文档：** `docs/plans/2026-06-10-market-status-design.md`

---

### Task 1: 后端 `/api/marketinfo_recent`

**Files:**
- Modify: `rt_monitor_app.py`（`get_margin_daily_recent` 附近仿写）

**Step 1:** 新增查询函数 `_query_marketinfo_recent(limit: int = 300) -> dict`

```python
def _query_marketinfo_recent(limit: int = 300) -> dict:
    """只读 marketinfo；limit=0 全量，否则 ORDER BY trade_date DESC LIMIT n。"""
    sql = """
        SELECT trade_date, amount, up_num, down_num, up_pct, total_num, med_pct_chg
        FROM marketinfo
        ORDER BY trade_date DESC
    """
    if limit and limit > 0:
        sql += " LIMIT %s"
    # 日期转 ISO 字符串；Decimal 转 float/int
```

**Step 2:** 新增路由

```python
@app.route('/api/marketinfo_recent', methods=['GET'], strict_slashes=False)
def get_marketinfo_recent():
    limit = request.args.get('limit', 300, type=int)
    if limit not in (0, 300, 1300):
        limit = 300  # 或 clamp：0/300/1300 三档
    cache_key = ('marketinfo_recent', int(limit))
    # _popup_series_cache_get/set，TTL 与 margin 一致
```

**Step 3:** 将 `/api/marketinfo_recent` 加入启动日志注册检查（若有同类列表）。

**Step 4:** 验证

```bash
curl -s "http://localhost:5000/api/marketinfo_recent?limit=3" | python -m json.tool
curl -s "http://localhost:5000/api/marketinfo_recent?limit=0" | python -c "import sys,json; d=json.load(sys.stdin); print(len(d['rows']))"
```

Expected: 首条 `trade_date` 为最新交易日；`limit=0` 行数约 8657。

**Step 5:** Commit

```bash
git add rt_monitor_app.py
git commit -m "feat(rt-monitor): add /api/marketinfo_recent for market status page"
```

---

### Task 2: 页面路由与模板骨架

**Files:**
- Modify: `rt_monitor_app.py` — `_render_rt_monitor_market_status_no_cache()`、`rt_monitor_market_status()`
- Create: `templates/rt_monitor_market_status.html`

**Step 1:** 路由

```python
@app.route('/rt-monitor/market-status', strict_slashes=False)
def rt_monitor_market_status():
    return _render_rt_monitor_market_status_no_cache()
```

渲染上下文与 `rt_monitor_margin` 一致：`rt_monitor_static_ver`、`rt_monitor_api_prefix`。

**Step 2:** 模板结构

- `body.rt-monitor.rt-monitor-market-status-page`
- 顶栏：标题 + 窗口按钮组 `#market-status-limit-300|1300|0` + 刷新按钮
- 主表：`#market-status-table`（thead 七列 + 空 tbody）
- 弹窗：`#market-status-chart-modal-overlay`（单 canvas，仿 up-num-ma5 modal）
- 脚本：`chart-stack.umd.js` + `rt_monitor_market_status.js`（defer）

**Step 3:** 浏览器验证路由

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/rt-monitor/market-status
```

Expected: `200`

**Step 4:** Commit

```bash
git add rt_monitor_app.py templates/rt_monitor_market_status.html
git commit -m "feat(rt-monitor): add market-status page route and template shell"
```

---

### Task 3: 主页侧栏链接

**Files:**
- Modify: `templates/rt_monitor.html`（期权T型报价 `<p>` 之后）

**Step 1:** 插入

```html
<p class="rt-monitor-niusan-link-wrap">
    <a href="{{ url_for('rt_monitor_market_status') }}" class="rt-monitor-niusan-link" target="_blank" rel="noopener noreferrer">市场状态</a>
</p>
```

**Step 2:** Commit

```bash
git add templates/rt_monitor.html
git commit -m "feat(rt-monitor): add market status link below opt-t-board"
```

---

### Task 4: CSS 布局与弹窗 shell

**Files:**
- Modify: `static/css/rt_monitor.css`

**Step 1:** 页面布局

```css
body.rt-monitor.rt-monitor-market-status-page { /* 100vh flex，表体滚动 */ }
.market-status-table-wrap { /* median-table-wrap 风格卡片 */ }
.market-status-th-chartable { cursor: pointer; }
```

**Step 2:** 弹窗 shell（复制 up-num-ma5 块改名）

```css
.bias-series-modal.market-status-chart-modal-shell { resize: both; overflow: auto; ... }
.bias-series-chart-wrap.market-status-chart-modal-chart-wrap { flex: 1; min-height: 160px; }
```

**Step 3:** `rt_monitor_app.py` 递增 `RT_MONITOR_STATIC_VER`

**Step 4:** Commit

```bash
git add static/css/rt_monitor.css rt_monitor_app.py
git commit -m "feat(rt-monitor): market-status page layout and chart modal CSS"
```

---

### Task 5: 前端表格与窗口切换

**Files:**
- Create: `static/js/rt_monitor_market_status.js`

**Step 1:** 基础工具

- `rtMonitorApiUrl()` — 读 `window.__RT_MONITOR_API_PREFIX__`
- `loadMarketStatusTable(limit)` — fetch `/api/marketinfo_recent?limit=`
- `renderMarketStatusRows(rows)` — 填充 tbody
- 格式化：`amount` 整数；`up_pct` 两位小数 + %；`up_pct` 着色 **>50 `.rise`，否则 `.fall`**；`med_pct_chg` 红涨绿跌

**Step 2:** 窗口切换

- `localStorage` key：`rt_market_status_limit_v1`，值 `300`|`1300`|`0`
- 顶栏按钮切换后重载表格；高亮当前选中项

**Step 3:** 交易日期排序

- 点击「交易日期」表头切换 ASC/DESC，前端对当前 `rows` 重排后重绘（不二次请求）

**Step 4:** 手动验证：打开 `/rt-monitor/market-status`，切换三档窗口，核对行数与着色。

**Step 5:** Commit

```bash
git add static/js/rt_monitor_market_status.js
git commit -m "feat(rt-monitor): market-status table render and limit switcher"
```

---

### Task 6: 图表弹窗（表头点击）

**Files:**
- Modify: `static/js/rt_monitor_market_status.js`
- Modify: `templates/rt_monitor_market_status.html`（若弹窗 DOM 需补 id）

**Step 1:** 表头绑定

- 成交额 / 上涨比例 / 涨跌中位数 `click` → `openMarketStatusChartModal(metricKey)`
- `metricKey`: `amount` | `up_pct` | `med_pct_chg`

**Step 2:** 弹窗逻辑（克隆 `initUpNumMa5Modal` / `initMarginRzmreIdxModal` 精简版）

- 拖动 header、`resize: both`、`save/load localStorage`（`rt_market_status_chart_modal_layout_v1`）
- `ResizeObserver` on chart wrap + modal shell
- Esc / 遮罩 / 关闭按钮

**Step 3:** Chart 渲染

- 使用当前已加载的 `rows`（与表格同窗口）；X 轴按 `trade_date` **升序**
- 单折线；Y 轴标题随 metric 变化
- 启用 zoom plugin（与超跌一致）

**Step 4:** 全量验证：选「全量」→ 点「上涨比例」→ 确认 Chart 点数 ≈ 8657。

**Step 5:** Commit

```bash
git add static/js/rt_monitor_market_status.js templates/rt_monitor_market_status.html
git commit -m "feat(rt-monitor): market-status draggable chart modal on header click"
```

---

### Task 7: 联调与收尾

**Files:**
- Modify: `rt_monitor_app.py`（文件末时间戳注释）

**Step 1:** 端到端检查清单

- [ ] 主页链接在新标签打开子页
- [ ] 默认 300 行，1300/全量正确
- [ ] 三列表头弹窗可拖可缩
- [ ] `up_pct` >50% 红，≤50% 绿
- [ ] API 异常时页面有提示

**Step 2:** 获取时间戳写入 `rt_monitor_app.py` 末行

```bash
python -c "from datetime import datetime; print(datetime.now().strftime('%Y-%m-%d %H:%M'))"
```

**Step 3:** Commit

```bash
git add rt_monitor_app.py
git commit -m "chore(rt-monitor): market-status E2E verified, update timestamp"
```

---

## 执行选项

计划已保存至 `docs/plans/2026-06-10-market-status.md`。

**1. 本会话 Subagent-Driven** — 按 Task 逐条实现并复核  
**2. 新会话 executing-plans** — 另开会话批量执行  

请告知选哪种，或直接说「开始实现」。
