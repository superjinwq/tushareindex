---
title: 牛散页「+」历史报告期展开修复
---

# 问题

`/rt-monitor/niusan` 主表「报告期」列「+」点击无反应。

# 根因

`#niusan-data-table` 的 `click` 委托里使用 `ev.target.closest(...)`。当点击落在按钮内 **文字节点**上时，`ev.target` 为 `Text` 节点，无 `closest`，抛出 `TypeError`，后续展开逻辑不执行。

# 修改

1. 在委托开头将 `ev.target` 归一化为 `Element`（`nodeType !== 1` 时取 `parentElement`），再 `closest`。
2. 同步 `RT_MONITOR_STATIC_VER` 便于与响应头 `X-RT-Monitor-Build` 对照。
