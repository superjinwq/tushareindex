# Quick Task 260923-u5l Summary

## 交付内容

- 新建 `opt_wall_510050.py`：独立 Flask 服务、同日只读 MySQL 查询、OI Wall 纯计算、`/api/oi-wall` 和 `/health`。
- 新建 `templates/opt_wall_510050.html`：四期限 OI 分布、墙位/Top 3、现货邻近、逐步计算追溯、数据质量和解释边界。
- 新建 `tests/test_opt_wall_510050.py`：聚合、并列墙位、零/空 OI、四期限、跨日拒绝、路由、安全日志和共享连接配置回归。
- 更新本地 `.planning/LESSONS.md` 与 `scripts/selfcheck_delivery.py`，增加 L059/L060；两文件原本均为未跟踪用户资产，因此不夹带进本任务提交。

## 红绿证据

- 初始测试：`ModuleNotFoundError: No module named 'opt_wall_510050'`。
- 真实库首次探测：捕获共享 `read_timeout` 重复关键字错误；新增回归测试先失败后修复。
- 敏感日志测试：确认旧实现会把 `top-secret` 写入日志；改为仅记录异常类型后通过。
- 聚焦测试最终：`7 passed`。

## 真实数据

- 数据日：2026-09-22；510050 `close_qfq=3.015`；96 行、96 标准合约、0 调整、0 缺失 OI。
- 当月 Call/Put Wall：3.10（95,320）/ 2.90（37,576）。
- 下月：3.10（72,257）/ 3.00（50,709）。
- 当季：3.50（34,099）/ 2.70（26,369）。
- 下季：3.50（4,396）/ 2.80（4,830）。

## 验证

- `python -m pytest tests/test_opt_wall_510050.py -q`：7 passed。
- `uv run ruff check ...`：通过。
- `uv run ty check --python ...`：通过。
- `python -X utf8 scripts/selfcheck_delivery.py`：PASS 159 / FAIL 0 / SKIP 4。
- `python -m pytest tests/ -q`：603 passed。
- `python -X utf8 scripts/selftest_guard_effective.py`：注入后 FAIL，字节级还原哈希一致。
- `python -X utf8 scripts/audit_scratch_retention.py --sweep`：完成，31 个本会话使用脚本续期。
- 运行态：`/health` database=ok；`/api/oi-wall` 四期限、96 行；浏览器点击柱体与期限切换均更新计算过程。
- 窄屏：390px 下 `scrollWidth=375 <= innerWidth=390`，无水平溢出。

## 运行状态

- 独立服务运行于 `http://127.0.0.1:5011/`。
- 未修改现有 5000 端口 T 型板，未写入数据库。
