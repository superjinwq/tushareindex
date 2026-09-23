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

## Follow-up 2026-09-23：展示口径修正

- 行权价改为由低到高；新增 `static/js/opt_wall_510050.js`，把排序、万单位格式和双墙选择做成可执行纯函数。
- Call Wall 与 Put Wall 默认同时展示完整计算及各自距现货公式；点击普通柱只替换对应方向的追踪。
- 所有 OI 展示统一为“万”且固定两位；数据质量中的合约行数继续显示整数。
- 测试先红：前端模块缺失导致 `MODULE_NOT_FOUND`；实现后聚焦测试 `8 passed`，全量测试 `604 passed`，ruff/ty 通过。
- 浏览器实测：当月行权价 2.65→3.60；Call Wall 3.10 / 9.53万 / +2.82%，Put Wall 2.90 / 3.76万 / -3.81%，双侧同时可见。
- 全局自检的代码与 lessons 守卫均通过（含新增 L061）；总计 PASS 151 / FAIL 8 / SKIP 5。8 个 FAIL 均因本次未启动的既有 5000 主服务及其页面探测，不属于独立 5011 页面回归；未擅自启动主服务。

## Follow-up 2026-09-23：Top 3 汇总补充

- 新增主图上方 `OI Wall / Top 3 汇总`，Call、Put 各自显示 Wall 行权价、Wall OI、距现货、Top 3 行权价和 Top 3 集中度。
- 当月真实页面核验：Call Top 3 为 3.10、3.20、3.60，集中度 64.58%；Put Top 3 为 2.90、2.95、3.00，集中度 53.46%。
- 新增 `summarizeSide` 前端纯函数及回归断言；全量测试仍为 `604 passed`，自检 L061 同步检查汇总区。
- 汇总区再补充 Top 1 集中度：当月 Call 为 25.13%，Put 为 18.81%；定义为该方向 Wall OI / 该方向全部 OI。
