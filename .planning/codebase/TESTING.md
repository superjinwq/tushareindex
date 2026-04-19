# Testing

**Analysis Date:** 2026-04-19

## Framework

- **pytest** 存在于 `pyproject.toml` dev 依赖组。
- 大型同步脚本多以 **手工运行 + 日志 + DB 抽样对账** 为主，而非全覆盖单测。

## Patterns observed

- 部分目录或实验脚本可能含独立测试；核心业务以「跑通一条增量 + 条数 reconcile」为验收。
- 新增财务/股东同步：建议保留 **阶段1 UPSERT** 后与 Tushare 抽样行数/键一致（与 `fina_indicator` `--reconcile` 思想一致）。

## Gaps

- `rt_monitor_app.py` 等极少见自动化 UI 测试；回归依赖浏览器手工与 API 抽检。
