# P3 — 首页域（main_bp）迁移

> 2026-06-17 执行；P4 迁 fina 域。

## 交付

| 项 | 路径 | 说明 |
|----|------|------|
| Blueprint | `rt_monitor/blueprints/main_bp.py` | `/`、`/rt-monitor` + 19 条首页 API |
| 实现 | `rt_monitor/monolith.py` | 业务逻辑暂留 monolith，bp 委托同名函数 |
| 剥离脚本 | `scripts/p3_strip_main_routes.py` | 从 monolith 删除已迁 `@app.route` |
| 验收 | `scripts/p3_verify.py` | 分步 `3.1`–`3.4` + `P1`/`P2`/`P4-smoke` + `all` |

## 同步修复（P3 验收暴露）

1. **`REPO_ROOT`**：`core/config.py` 指向仓库根（含 `templates/`、`static/`），非 `rt_monitor/` 包目录。
2. **模板 `url_for`**：Blueprint 端点为 `blueprint.endpoint` 形式；`rt_monitor.html` / `niusan_config` 子页链接已更新。
3. **opt_t_board service**：补 `defaultdict`、`datetime` import（P2 抽取遗漏）。
4. **`create_app()`**：Blueprint 仅注册一次；`p3_verify all` 复用同一 app 实例。

## 验收（2026-06-17）

- [x] `python scripts/p3_verify.py 3.1` — 首页 + rt_median
- [x] `python scripts/p3_verify.py 3.2` — limit_stats 簇
- [x] `python scripts/p3_verify.py 3.3` — ETF bias/ma
- [x] `python scripts/p3_verify.py 3.4` — idx_minute / top_moves / ma_sr 等
- [x] `python scripts/p3_verify.py all` — 含 P1/P2/P4-smoke，**PASS**
- [x] 路由 41 条（非 static）、无重复 rule
- [x] 7 个 Blueprint：`main`, `margin`, `market_status`, `sw`, `index`, `opt_t_board`, `niusan`
- [ ] 本机重启 `python rt_monitor_app.py` 浏览器 smoke（用户）
- [ ] Commit（待用户要求）

## P4 下一步

- fina 页面 + `/api/fina_*` 迁入 `fina_bp` / `services/fina*.py`
- sw/index 查询 helper 可随 fina 批量读一并抽取

*计划版本：2026-06-17 15:48*
