# P2 — sw / index / opt_t_board / niusan 域迁移

> 2026-06-17 执行；P3 起迁首页 API 簇。

## 交付

| 域 | services | blueprints | monolith 残留 |
|----|----------|------------|---------------|
| niusan | `rt_monitor/services/niusan.py` | `niusan_bp.py` | 无 |
| opt_t_board | `rt_monitor/services/opt_t_board.py` | `opt_t_board_bp.py` | 无 |
| sw | （实现仍在 monolith `api_sw_*`） | `sw_bp.py` | 查询/聚合 ~3k 行，待 P2.1 或随 fina 拆 |
| index | （实现仍在 monolith `api_index_*`） | `index_bp.py` | 同上 |

## 验收（2026-06-17）

- [x] `create_app()`：八路由各 `count=1`（sw/index/opt/niusan + 三 API）
- [x] `py_compile` 通过
- [ ] 重启 `python rt_monitor_app.py` 后浏览器 smoke（用户本机）
- [ ] Commit（待用户要求）

## P2.1 可选

- 将 `api_sw_industry_stats` / `api_index_industry_stats` 及 helper 块迁入 `services/sw_industry.py`（需同步迁 fina 批量读依赖或抽 `services/fina_batch`）

*计划版本：2026-06-17 15:17*
