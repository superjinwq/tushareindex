# 市场状态页设计说明

**日期**：2026-06-10  
**状态**：已确认（用户批准）  
**关联**：`rt_monitor_app.py`、`templates/rt_monitor.html`、`templates/rt_monitor_market_status.html`、`static/js/rt_monitor_market_status.js`

## 目标

1. 行情监控主页「期权T型报价」下方新增 **市场状态** 超链接（新标签打开）。
2. 新增独立页 `/rt-monitor/market-status`：只读 `stock_tushare.marketinfo`，表格展示 7 列，按 `trade_date` 降序。
3. 表头点击打开 **可拖动、可 resize** 的折线弹窗（成交额 / 上涨比例 / 涨跌中位数），交互对齐融资占比、超跌弹窗范式。

## 已锁定口径

| 项 | 约定 |
|----|------|
| 数据源 | `marketinfo` 只读 |
| 表格列 | `trade_date`、`amount`（亿）、`up_num`、`down_num`、`up_pct`（%）、`total_num`、`med_pct_chg`（%） |
| 默认排序 | `trade_date` DESC |
| 窗口切换 | 顶栏 `300`（默认）/ `1300` / `全量`；写入 `localStorage` |
| 全量规模 | 约 8657 行；表格与图表均使用同一窗口（全量时图表亦 8657 点） |
| 可点表头 | 仅 **成交额**、**上涨比例**、**涨跌中位数** |
| `up_pct` 着色 | **> 50%** 红色（`.rise`）；**≤ 50%** 绿色（`.fall`） |
| `med_pct_chg` 着色 | 常规红涨绿跌：正值 `.rise`，负值 `.fall`，零无色 |
| 链接样式 | `rt-monitor-niusan-link-wrap` + `target="_blank"`，与侧栏其它外链一致 |

## 方案

**采用方案 A**：独立子页 + 专用 API + 独立 JS（`rt_monitor_market_status.js`），不膨胀 `rt_monitor.js`。

| 方案 | 优点 | 缺点 |
|------|------|------|
| A 独立子页（采用） | 与财报/两融一致；易维护 | 新增 3~4 文件 |
| B SSR 首屏 | 首屏快 | 切换窗口仍需 API，架构分裂 |
| C 主监控内嵌 | 改动集中 | 不符合「新页面」诉求 |

## 路由与文件

| 路径 | 说明 |
|------|------|
| `GET /rt-monitor/market-status` | 页面路由 `rt_monitor_market_status()` |
| `GET /api/marketinfo_recent?limit=` | 表格与弹窗共用数据；`limit=0` 表示全量 |

新增/修改文件：

- `templates/rt_monitor.html` — 侧栏链接
- `templates/rt_monitor_market_status.html` — 页面骨架 + 单图表弹窗 overlay
- `static/js/rt_monitor_market_status.js` — 表格渲染、窗口切换、弹窗 Chart
- `static/css/rt_monitor.css` — 页面与弹窗 shell 样式（或页内 `<style>` 少量布局）
- `rt_monitor_app.py` — 路由、API、渲染 helper、`RT_MONITOR_STATIC_VER` 递增

## 后端 API

`GET /api/marketinfo_recent?limit=300`

- `limit`：默认 `300`；`1300`；`0` = 全量（无 SQL LIMIT）
- SQL：

```sql
SELECT trade_date, amount, up_num, down_num, up_pct, total_num, med_pct_chg
FROM stock_tushare.marketinfo
ORDER BY trade_date DESC
-- limit > 0 时追加 LIMIT n
```

- 响应：

```json
{
  "rows": [{ "trade_date": "2026-06-09", "amount": 26633, "up_num": 3321, ... }],
  "limit": 300,
  "error": null
}
```

- 短 TTL 缓存（与 `margin_daily_recent` 同类，约 60s），key `('marketinfo_recent', limit)`

## 前端页面

```
┌──────────────────────────────────────────────────┐
│ 市场状态          [300 | 1300 | 全量]    [刷新]   │
├──────────────────────────────────────────────────┤
│ 可滚动表格（tbody 原生滚动，整页不纵向溢出）        │
└──────────────────────────────────────────────────┘
```

- `body.rt-monitor.rt-monitor-market-status-page`
- 复用 `dashboard.css` + `rt_monitor.css` token（浅灰底、卡片表、13px 数据字）
- 表头可点列：`cursor: pointer` + `title` 提示「点击查看序列图」
- 交易日期列：点击切换 ASC/DESC（默认 DESC）

## 图表弹窗

- 单一 overlay：`#market-status-chart-modal-overlay`（按点击列切换标题与序列）
- Shell：复制 `.up-num-ma5-modal-shell`（`resize: both`、`overflow: auto`）
- 拖动：header `mousedown` 逻辑（与 `initMarginRzmreIdxModal` 一致）
- `localStorage`：`rt_market_status_chart_modal_layout_v1`
- `ResizeObserver` → `Chart.resize()`
- 图表：Chart.js 折线；X 轴 `trade_date` **升序**；数据来自当前窗口的 `/api/marketinfo_recent` 响应（前端反转或 API 另提供 `order=asc` 供图表 — 实现时二选一，表格仍 DESC）
- Zoom：与超跌弹窗同款 `chartjs-plugin-zoom` 横向 pan/zoom
- 关闭：× / Esc / 点遮罩

弹窗标题与 Y 轴：

| 表头 | 标题示例 | 字段 | Y 轴单位 |
|------|----------|------|----------|
| 成交额 | 成交额（亿） | `amount` | 亿 |
| 上涨比例 | 上涨比例（%） | `up_pct` | % |
| 涨跌中位数 | 涨跌中位数（%） | `med_pct_chg` | % |

## 错误处理

- API 失败：表格区错误条；弹窗内「加载失败」
- 空表：表格「暂无数据」
- 全量模式：接受 8657 点 Chart 渲染略慢（用户确认）

## 验收标准

1. 主页「期权T型报价」下可见「市场状态」链接，新标签打开子页。
2. 子页默认 300 行，切换 1300/全量后表格行数正确，刷新后保持窗口选择。
3. 三列表头可开弹窗，拖动/resize/关闭正常，全量下图表点数与表格一致。
4. `up_pct` 单元格：>50% 红，≤50% 绿；`med_pct_chg` 红涨绿跌。
5. `RT_MONITOR_STATIC_VER` 已递增，静态资源无缓存错位。
