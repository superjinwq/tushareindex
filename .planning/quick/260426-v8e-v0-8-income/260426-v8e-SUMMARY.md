# Quick 260426-v8e — 完成摘要

## 结果

- 后端（此前已合）：`_fina_arturn_attach_quarter_op_yoy_excess_gr_yoy` 写入 `v0..v8`；fast_path 已占位 `v*`。
- 前端：`rt_monitor_fina.js` 表头分组 + 第二行 `v0..v8`、`tbody` 9 格、筛选 matcher、`getSortCell` / `compareRows` / `isSortable`、`computeFinaMetricFilterKeyTitle`。
- 样式：`rt_monitor.css` `#fina-arturn-table` 细线 NOT 列表与粗线列号自 `m0` 起整体 +9，thead-group 增至 `nth-child(23)`。
- 版本：`RT_MONITOR_STATIC_VER` → `20260426-18`（`rt_monitor_app.py` / `web_dashboard.py` 同步）。
- 规则：`.cursor/rules/rt-monitor-fina-arturn-layout.mdc` 列数与分组边界表与当前 CSS 对齐（含 `invchg` / `v0`）。

## 验证命令

- `python -m py_compile rt_monitor_app.py web_dashboard.py`
- `node --check static/js/rt_monitor_fina.js`

## 业务核对（供用户确认）

1. `u ≤ r` 或缺数：格为 `--`（与后端 `None` 一致）。
2. 单季营业利润为负：不写差值（`--`）；无 `income.operate_profit` 时仅按 `u>r` 写差值（后端注释已说明）。
3. `u`、`r` 与 `operate_profit` 单季拆解的口径是否需完全对齐 Tushare 文档，若有偏差是否接受以 `income` 行为准。
