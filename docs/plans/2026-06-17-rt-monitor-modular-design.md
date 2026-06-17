# rt_monitor 模块化架构设计

**文档日期**：2026-06-17  
**状态**：已确认（brainstorming 定稿）  
**范围**：方案 C — 单进程 Flask + `rt_monitor/` 按域分包 + 批处理独立脚本  

**明确排除（本里程碑）**：

- `first_qfq_new_high` 落地表及 fina 页增列（暂缓，架构预留接口即可）
- 多端口 / 多 Flask 进程
- 全量一次性拆完 `rt_monitor_app.py`（~1.8 万行）
- 框架替换（FastAPI / Django 等）

---

## 1. 背景与目标

### 1.1 现状

| 层级 | 现状 |
|------|------|
| 后端 | 几乎全部在 `rt_monitor_app.py`（~17900 行） |
| 前端 | 已按页拆分：`rt_monitor_fina.js`、`rt_monitor_market_status.js`、`rt_monitor.js` 等 |
| 模板 | 每页独立 HTML |
| 数据同步 | 根目录独立 py（`fina_indicator.py`、`margin_sum.py` 等） |
| 部署入口 | `run_rt_monitor.bat` → `python rt_monitor_app.py` |

### 1.2 目标

1. **可维护**：新页面/新 API 不再往 monolith 堆代码；按域修改、按域 review。
2. **稳健**：仍 **单进程、单端口**；单实例锁、预热、缓存行为不变。
3. **渐进**：Strangler 迁移，每阶段可独立验收与回滚。
4. **复用**：批处理脚本与 Web 共用 `services` 内纯函数；外部 trial 脚本不再 `import rt_monitor_app` 巨模块。

---

## 2. 架构原则

### 2.1 三层职责

```text
Blueprint（HTTP）  →  Service（业务/SQL/组装 JSON）  →  Core（DB/缓存/配置/锁）
         ↑                        ↑
    不扫全市场 K 线          可被 Job 脚本 import
```

| 层 | 职责 | 禁止 |
|----|------|------|
| **Blueprint** | 路由、参数校验、HTTP 状态码 | 数百行 SQL、全市场扫描 |
| **Service** | 查询、attach 列、缓存 key | 依赖 Flask `request` 全局（应用工厂注入参数） |
| **Core** | `DB_CONFIG`、连接、TTL 缓存、单实例锁、`RT_MONITOR_*` | import services / blueprints |

**依赖方向（单向）**：`core` ← `services` ← `blueprints` ← `app_factory`

### 2.2 数据流（与 future first_qfq 一致）

```text
批处理 *.py（日终/手动）  →  MySQL 落地表
Web Service（只读）       →  JOIN / 快照
Blueprint                 →  JSON → 现有 static/js
```

Web **不写**行情业务表（现有声明不变）；例外：`ma_sr_levels.json`、启动 DDL 等保留在 core/startup。

### 2.3 web_dashboard 策略（用户确认：方案 A）

- **唯一完整入口**：行情监控以 `python rt_monitor_app.py`（或 `run_rt_monitor.bat`）为准。
- `web_dashboard.py` 若保留与 rt-monitor 重叠路由，**仅**改为 `from rt_monitor.services.* import ...`，**禁止**在 dashboard 内维护第二套 SQL。
- `RT_MONITOR_STATIC_VER` 仍须与主入口同步（直至 dashboard 不再暴露 rt-monitor 页）。
- 文档与日志中明确：「完整 API 列表以 `rt_monitor_app` 注册为准」。

---

## 3. 目录结构（目标态）

```text
rt_monitor/
  __init__.py
  app_factory.py              # create_app(), register_blueprints, warmup, template globals
  core/
    config.py                 # RT_MONITOR_PORT, API_PREFIX, STATIC_VER, env
    instance_lock.py          # mutex / flock / auto-kill 逻辑
    cache.py                  # TTL LRU（替代散落的全局 dict）
    db.py                     # DB_CONFIG, get_db_connection, DB_CONFIG_TUSHARE
  blueprints/
    main_bp.py                # /, /rt-monitor, limit_stats, etf_bias, ...
    fina_bp.py                # P4 迁移
    margin_bp.py              # P1
    market_status_bp.py       # P1
    sw_bp.py                  # P2
    index_bp.py               # P2
    opt_t_board_bp.py           # P2
    niusan_bp.py              # P2
  services/
    fina_arturn.py            # P4：_query_fina_arturn_table 及 attach 链
    margin.py
    market_status.py
    sw_industry.py
    index_industry.py
    opt_t_board.py
    niusan.py
    main_dashboard.py         # rt_median, limit_stats, etf 相关

rt_monitor_app.py             # 薄入口：锁 + create_app + socketio.run（目标 ~80 行级）
run_rt_monitor.bat            # 不变
templates/  static/           # 不变（按页已有拆分）
```

根目录 **批处理脚本**（`fina_indicator.py`、`first_qfq_new_high.py` 等）保持独立；需要共享逻辑时 import `rt_monitor.services.*`。

---

## 4. 迁移阶段（Strangler）

| 阶段 | 内容 | 验收要点 |
|------|------|----------|
| **P0** | 建 `rt_monitor/` 包 + `create_app()`；路由仍委托原函数（re-export / 薄 wrapper） | 所有现有 URL、JSON、响应头不变；bat 启动正常 |
| **P1** | 迁 **market_status**、**margin** blueprint + service | 两页 + 对应 API 快照对比 |
| **P2** | 迁 **sw、index、opt_t_board、niusan** | 同上 + 申万预热日志 |
| **P3** | 迁 **main 首页 API 簇**（limit_stats、etf_bias、ma_signal 等） | 首页轮询、SocketIO（若有）正常 |
| **P4** | 迁 **fina_arturn**（**单独里程碑**，可再拆 PLAN） | 财报表 240s 内加载、fast_path 与 attach 一致 |

**规则**：

- 新功能 **只允许** 进 `rt_monitor/blueprints` + `services`（P0 完成后）。
- 每阶段 **独立 commit**，便于 revert。
- **零 UI 变更**（除非单独需求）；财报表 layout 遵守 `rt-monitor-fina-arturn-layout.mdc`。

---

## 5. 必须迁移或集中管理的横切关注点

### 5.1 启动与运行时

| 项 | 处理方式 |
|----|----------|
| Flask + SocketIO | `app_factory.create_app()` 创建 `app`；`socketio` 在 factory 或 app 模块级初始化，**单例** |
| 单实例锁 | `core/instance_lock.py`；`rt_monitor_app.py` 启动前调用 |
| `RT_MONITOR_AUTO_KILL_PREVIOUS` | 保留 env 语义 |
| 后台预热线程 | 仅在 factory 启动路径注册 **一次**：fina arturn、sw L1、ma_signal |
| `atexit` / 信号 | 随 instance_lock 迁入 core |
| 模板全局 | `rt_monitor_static_ver`、`rt_monitor_api_prefix` 在 factory 注入 |
| `TEMPLATES_AUTO_RELOAD` | 保持开发体验 |

### 5.2 进程内缓存

- 现网模块级 TTL dict（如 `_fina_indicator_metric_table_cache`、`_STK_FACTOR_PRO_MAX_D_CACHE`）迁入 `core/cache.py` 或域 service 模块，**key/TTL 文档化**。
- 迁移期允许 `rt_monitor_app`  re-export 旧全局名，避免 silent 双缓存。

### 5.3 外部 import 兼容

| 调用方 | 策略 |
|--------|------|
| `roic_diy_enhanced_trial.py` 等 | 改 import 为 `rt_monitor.services.fina_*`；或 P0 末提供 `rt_monitor/compat.py` 转发 |
| `rt_monitor_sw_standalone.py` | 本期不实现「瘦加载」；注释更新为「仍加载 create_app 全量」 |

### 5.4 dist bundle

- `dist/rt_monitor_sw_bundle/` **不在 P0–P3 自动同步**；设计注明：bundle 为历史拷贝，正式开发以仓库根 + bat 为准；若仍部署 bundle，单独打 sync 任务（P4 后或按需）。

### 5.5 日志

- 继续使用 `logger_config.get_logger`；模块名区分 domain，**不**为每页新增独立 log 文件。

---

## 6. 验证与回滚

### 6.1 每阶段必做（无 CI）

1. `netstat -ano`：目标端口 **仅一个** `python … rt_monitor_app.py` LISTEN。
2. 浏览器/API：`X-RT-Monitor-Build`、`X-RT-Monitor-App` 与当前 `RT_MONITOR_STATIC_VER` 一致。
3. 关键 API JSON 结构 + 行数量级对比（阶段相关 URL 列表见实施 PLAN）。
4. 财报表（P4 前若未动 fina，仍 smoke 一次确保 P0 re-export 未破坏）。
5. 启动日志：预热线程是否按预期输出。

### 6.2 回滚

- 单阶段 revert；P0 失败则删除 `rt_monitor/` 包、恢复 monolith 启动即可。

---

## 7. 与 first_qfq 的关系（暂缓）

- 不在本里程碑建表、不改 fina 前端。
- 架构已预留：**Job 写落地表 → `services/fina_arturn` attach 只读列**；future 不修改 Blueprint 注册方式。

---

## 8. 成功标准

- [ ] P0 完成：`rt_monitor_app.py` 显著变薄，行为与重构前一致。
- [ ] P1 完成：market_status、margin 代码位于 `rt_monitor/`，无 dashboard 专属 SQL 分叉。
- [ ] 新页面开发模板 documented：blueprint + service + template + js 四件套。
- [ ] `web_dashboard` 重复 API（若触及）已改为 import service。
- [ ] 无新增多实例 / 端口冲突类 regression。

---

## 9. 决策记录

| 决策 | 选项 | 理由 |
|------|------|------|
| 总体架构 | **C** 单进程分包 | 避免多实例；与现有 bat/局域网部署一致 |
| web_dashboard | **A** 收敛到 service import | 消除双轨 SQL；主入口唯一 |
| first_qfq @ fina | **暂缓** | 降低首期风险 |
| fina 拆分 | **P4 独立里程碑** | 体量最大，attach 链复杂 |
| 迁移节奏 | **新功能新包 + 旧代码碰到再拆** | 可控、可回滚 |

---

*设计确认：用户 2026-06-17 同意方案 C 与 web_dashboard 方案 A；first_qfq 上 fina 暂缓。*
