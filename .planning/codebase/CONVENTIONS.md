# Conventions

**Analysis Date:** 2026-04-19

## Source of truth

- 仓库细则：`.cursor/rules/jinwq.mdc`（camelCase 标识符、Python 3.12、MySQL 8.4、日期 DATE/`YYYYMMDD` 口径）。
- Tushare 入库生成规范：`.cursor/skills/tushare-to-mysql/SKILL.md`（分页、fields、限速 0.2s、NaN→NULL）。

## Python

- **命名**：变量/函数 **camelCase**（本项目约定，与常见 PEP8 snake_case 并存时需跟仓库）。
- **类型**：关键路径逐步类型提示；脚本偏工程化可读。
- **日志**：与 py 同名日志、轮转、Windows 安全 Handler（常驻脚本见 `python-daemon-exit-and-log.mdc`）。
- **API**：`_wait_tushare_api_slot` / 全局限流，避免多线程抢跑打满接口。

## SQL

- 字符集 `utf8mb4`，排序 `utf8mb4_0900_ai_ci`（规则已载明）。
- 财务 PIT：`fina_indicator` 唯一键含 `ann_date`、`update_flag` 等（与现脚本一致）。

## Git / 规划

- GSD 产物置于 `.planning/`；若 `commit_docs: true` 则纳入版本控制。
