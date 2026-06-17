# rt_monitor 模块化 — 实施计划（P0–P1）

> **For agentic workers:** 按任务顺序执行；每 Task 结束 commit（用户要求时再 push）。P2–P4 在本 PLAN 验收通过后另开 PLAN。

**Goal:** 建立 `rt_monitor/` 包与 `create_app()` 工厂（P0），并率先迁出 market_status、margin 两域（P1），行为与现网一致。

**Architecture:** 单进程 Flask + Blueprint/Service/Core 分包；`rt_monitor_app.py` 薄入口；web_dashboard 重复 API 仅 import service（方案 A）。

**Tech Stack:** Python 3.12, Flask 3.x, flask-socketio, pymysql, 现有 templates/static。

**排除:** first_qfq 上 fina；fina_arturn 全量迁移（P4）；dist bundle 同步。

---

## 文件映射（P0–P1）

| 新建 | 职责 |
|------|------|
| `rt_monitor/__init__.py` | 包标记 |
| `rt_monitor/app_factory.py` | `create_app()`, blueprint 注册, warmup, template globals |
| `rt_monitor/core/config.py` | 从 app 抽出 `RT_MONITOR_*`, `DB_CONFIG*` |
| `rt_monitor/core/instance_lock.py` | 单实例 / auto-kill |
| `rt_monitor/core/cache.py` | `_small_ttl_cache_*` 等通用封装（先迁接口，实现可薄包装旧 dict） |
| `rt_monitor/core/db.py` | 连接池 / pymysql 直连 |
| `rt_monitor/blueprints/margin_bp.py` | `/rt-monitor/margin`, `/api/margin_*` |
| `rt_monitor/blueprints/market_status_bp.py` | `/rt-monitor/market-status`, `/api/marketinfo_*` 等 |
| `rt_monitor/services/margin.py` | `_query_margin_daily_recent` 等 |
| `rt_monitor/services/market_status.py` | marketinfo 相关 query |

| 修改 | 职责 |
|------|------|
| `rt_monitor_app.py` | 删已迁代码，保留 re-export + `socketio.run` |
| `web_dashboard.py` | （P1 末）margin 路由改 import `rt_monitor.services.margin` |

---

## Task 1: P0 — 脚手架与 create_app（行为不变）

**Files:**
- Create: `rt_monitor/` 包 skeleton + `app_factory.py`
- Modify: `rt_monitor_app.py`（改为调用 `create_app()`）

- [x] **Step 1:** 创建 `rt_monitor/core/config.py`，迁入 `RT_MONITOR_PORT`, `RT_MONITOR_STATIC_VER`, `RT_MONITOR_API_PREFIX`, `DB_CONFIG`, `DB_CONFIG_TUSHARE` 等（从 app 复制，不改值）。
- [x] **Step 2:** 创建 `rt_monitor/core/instance_lock.py`，迁入单实例锁与 `RT_MONITOR_AUTO_KILL_PREVIOUS` 逻辑。
- [x] **Step 3:** 创建 `rt_monitor/app_factory.py`：`create_app()` 实例化 Flask、`SocketIO(app)`、注册**现有全部路由**（P0：`rt_monitor/monolith.py` 保留全部 `@app.route`）。
- [x] **Step 4:** `rt_monitor_app.py` 仅保留：`create_app()`、`socketio`、启动块、预热调用、roic re-export。
- [x] **Step 5:** `python rt_monitor_app.py` 启动；`curl -I http://127.0.0.1:5000/rt-monitor/fina` → `X-RT-Monitor-Build: 20260617-1`。
- [x] **Step 6:** 验证单实例：二次 `ensureSingleInstanceOrExit` → exit 1。
- [ ] **Step 7:** Commit: `refactor(rt_monitor): P0 app factory scaffold`（待用户要求）

**P0 验收 API（JSON 键存在即可，不要求逐字段 diff）：**

- `GET /api/limit_stats`
- `GET /api/fina_arturn_days_table?end_date=<最近一期>`
- `GET /api/sw_industry_stats?level=l1`

---

## Task 2: P0 — 预热与模板全局

**Files:**
- Modify: `rt_monitor/app_factory.py`

- [x] **Step 1:** 预热三函数在 `app_factory.runStartupHooks()` 调用，`_warmupStarted` 防重复。
- [x] **Step 2:** context processor 暂留 `monolith.py`（随 `create_app()` import 生效；P1 可再迁入 factory）。
- [x] **Step 3:** 冷启动 smoke：42 路由、三 API 200、无 duplicate warmup 日志（仅 GBK 控制台 emoji 告警，非阻塞）。
- [ ] **Step 4:** Commit: `refactor(rt_monitor): P0 warmup and template globals`（待用户要求）

---

## Task 3: P1 — margin 域迁移

**Files:**
- Create: `rt_monitor/services/margin.py`, `rt_monitor/blueprints/margin_bp.py`
- Modify: `rt_monitor_app.py` 或 factory 注册 margin_bp

- [x] **Step 1:** 将 `_query_margin_daily_recent`、`_query_margin_rzmre_idx_series` 及 margin 路由函数迁入 `services/margin.py`（纯函数 + 可选 conn 参数）。
- [x] **Step 2:** `margin_bp` 注册 `/rt-monitor/margin`, `/api/margin_daily_recent`, `/api/margin_rzmre_idx_series`（路径与现网完全一致）。
- [x] **Step 3:** 从 monolith 删除已迁函数（popup 缓存抽到 `core/popup_cache.py`）。
- [x] **Step 4:** `queryMarginDailyRecent(limit=2)` smoke 通过；路由 count=1。
- [ ] **Step 5:** Commit: `refactor(rt_monitor): P1 margin domain`（待用户要求）

---

## Task 4: P1 — market_status 域迁移

**Files:**
- Create: `rt_monitor/services/market_status.py`, `rt_monitor/blueprints/market_status_bp.py`

- [x] **Step 1:** 迁入 `/rt-monitor/market-status` 页面路由与 `/api/marketinfo_recent`。
- [x] **Step 2:** 注册 blueprint；删除 monolith 重复代码。
- [x] **Step 3:** `queryMarketinfoRecent` smoke；路由 count=1。
- [ ] **Step 4:** Commit: `refactor(rt_monitor): P1 market_status domain`（待用户要求）

---

## Task 5: P1 — web_dashboard 收敛（方案 A）

**Files:**
- Modify: `web_dashboard.py`

- [x] **Step 1:** margin 相关路由改为 `from rt_monitor.services.margin import query_margin_daily_recent`。
- [x] **Step 2:** 文件头注释更新：完整 rt-monitor API 以 `rt_monitor_app.py` 为准。
- [x] **Step 3:** `from web_dashboard import get_margin_daily_recent` import smoke。
- [ ] **Step 4:** Commit: `refactor(web_dashboard): margin via rt_monitor.services`（待用户要求）

---

## Task 6: P1 验收与文档

- [x] **Step 1:** 更新 `AGENTS.md`：`rt_monitor/` 包说明。
- [x] **Step 2:** P1 验收：五路由各 count=1；margin/marketinfo service 查询 OK。
- [ ] **Step 3:** 用户确认后，另开 `2026-06-XX-rt-monitor-modular-plan-p2.md`（sw/index/opt/niusan）。

---

## P2–P4 预告（不在本 PLAN 执行）

| 阶段 | 域 | 备注 |
|------|-----|------|
| P2 | sw, index, opt_t_board, niusan | 申万预热已在 P0 |
| P3 | main 首页 API 簇 | limit_stats, etf_bias, ma_signal |
| P4 | fina_arturn | 独立 PLAN；attach 链分文件 |

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 循环 import | 严格 core ← services ← blueprints |
| 双缓存 | P0 不拆缓存实现，只搬 factory；P1 起逐域迁 cache key |
| 多实例 | 每阶段 netstat 检查；沿用 checklist 文档 |
| roic 脚本 break | P0 末加 compat re-export 或列清单改 import |

---

*计划版本：2026-06-17，对应设计 `2026-06-17-rt-monitor-modular-design.md`*
