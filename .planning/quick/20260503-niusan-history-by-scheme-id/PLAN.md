---
title: 牛散各方案「+」历史与当前主表方案一致
---

# 现象

仅「最后添加/当前 activeId 为邓晓峰」时「+」有数据；切到章建平/葛卫东/魏巍时弹窗「无更早报告期数据」。

# 根因

`_query_niusan_floatholder_history` / `_query_niusan_floatholder_detail` 一直用 `_niusan_holder_params()`（启用方案），与主表按 `scheme_id` 切换的关联人不一致。

# 修改

- `_niusan_resolve_holder_hw_hp(for_scheme_id)` 统一解析 hw/hp；持有页查询复用。
- API 增加可选 `scheme_id`；前端维护 `niusanViewSchemeId`，请求历史/明细带参；`histCache`/`detailCache` 按 `schemeId + ts_code` 隔离。
