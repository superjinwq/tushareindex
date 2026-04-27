# Quick: 财报表冻结列「股票名称」打开固定资产速览

## 目标

- 冻结区第二列与「固定资产/最新季 f0」共用同一套：`.fina-fixed-assets-clickable` + `openFinaFixedAssetsModal` + 既有 `#fina-fixed-assets-modal-overlay`。
- 不新增第二套弹窗或重复 HTML 模板；点击监听改为委托 `#fina-arturn-tbody-scroll`，一次绑定覆盖左右两表。

## 实施

1. `buildRowHtml` 冻结行：`sec_name` 格内包一层与 f0 相同 class/data 的 `span`。
2. `initFinaMetricPanel`：监听从 `#fina-arturn-tbody` 迁到 `ids.splitWrap`（`#fina-arturn-tbody-scroll`）。
3. `RT_MONITOR_STATIC_VER` 20260426-20；`rt_monitor_app.py` / `web_dashboard.py` 同步。
