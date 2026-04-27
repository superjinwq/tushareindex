# Quick: 单季度毛利率新增「新高」与「环比」

## 目标

- 在「单季度 毛利率(%)」分组内新增 2 列：`m_high`（新高）、`m_qoq`（环比连增）。
- `m_high` 口径与扣非净利润新高一致：`m0` 与展示季 `m0..m8` 最大值比较。
- `m_qoq` 口径：从 `m0 > m1` 起连续计数，若 `m1 > m2` 继续 +1，最大 8。

## 改动范围

1. 后端：`_fina_arturn_attach_quarter_margins` 计算并回填 `m_high/m_qoq`。
2. 前端：表头分组 colspan、列渲染、筛选、排序、列说明同步。
3. 版本：`RT_MONITOR_STATIC_VER` 升级到 `20260427-21`。
