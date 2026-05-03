---
status: complete
---

# 结果

- `templates/rt_monitor_niusan.html`：`#niusan-data-table` 点击处理前将 `event.target` 归一化为元素节点，再执行 `.niusan-stock-name-btn` / `.niusan-sort-btn` / `.niusan-expand-btn` 分支。
- `rt_monitor_app.py`、`web_dashboard.py`：`RT_MONITOR_STATIC_VER` → `20260503-20`。

请重启 `rt_monitor_app.py` 后刷新牛散页验证：点击「+」应请求 `/api/niusan_floatholder_history` 并展开子行。
