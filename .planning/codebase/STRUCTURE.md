# Repository Structure

**Analysis Date:** 2026-04-19

## Top-level

| 区域 | 说明 |
|------|------|
| `*.py`（根目录） | 业务脚本：同步、回测辅助、监控入口 |
| `templates/`、`static/` | Flask 前端资源 |
| `sql/` | 散落 DDL / 迁移参考 |
| `.cursor/`、`AGENTS.md` | 编辑器规则与 Agent 说明 |
| `.cursor/get-shit-done/` | GSD 工作流与 `gsd-tools.cjs` |
| `qlib/` | 上游量化库副本（子树大，非日常改动核心） |

## Naming

- 入库脚本常以接口名或业务命名：`fina_indicator.py`、`margin_sum.py` 等。
- 日志文件与脚本同名（`jinwq` 约定）。

## High-churn files

- `rt_monitor_app.py`：八千行量级，改动需克制、遵守 `myuiux` 等规则。
- `fina_indicator.py`：两千行量级，财务同步核心。
