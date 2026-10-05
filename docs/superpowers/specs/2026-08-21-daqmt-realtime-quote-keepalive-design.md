# 大 QMT 全推长稳保活设计

**日期：** 2026-08-21  
**目标：** 交易日 09:00–15:30 左右，用大 QMT 桥接把 7214 全推挂住，不中断；本阶段**不写库**，不替换 `rt_k`。  
**入口：** `d:\cursor\tushareindex\大QMT实时行情.py`（Python 3.12 独立进程，用法对齐 `tusharert个股实时行情.py`）。

## 背景（已实测，不重复论证）

- 有推送时热路径正常（约 50ms/批）。
- 14:19 策略进程 GIL/原生全推卡住时，RPC 与推送一起停；`.bat` 或 Redis 挡不住。
- 14:25 后 RPC 恢复、批次数钉死：combo 还在、QMT 句柄已哑。库内「静默幂等 replay」不调用 `ContextInfo.unsubscribe_quote`，无法自愈。
- 禁止用 6876/`['SH','SZ']` 的 `get_full_tick` 打底救人。

## 方案

职责拆开：**桥接只变瘦（回调合并最新瘦字段）**；**假活重建只放在脚本里**（脚本有交易日历，QMT 策略进程没有）。

不上 Redis。RPC 仍 ZMQ `127.0.0.1:15573`，全推 PUB `15574`。

```
QMT「大QMT桥接」
  subscribe_whole_quote(7214)
  on_push → 按代码合并最新瘦字段
  发送线程 → encode + ZMQ PUB :15574

大QMT实时行情.py
  SUB 收增量（只更新计数与 lastPushTime）
  窗口内：ping 通 且 ≥15s 无增量 → 真退订 → 再订（prime=False）
```

## 假活窗口与阈值

- **生效（本地时区，半开区间）：** `[09:15, 11:30)`、`[13:00, 15:00)`（含 09:15–09:25 集合竞价；11:30 起与 15:00 起不算窗口）。
- **阈值 N = 15 秒**（躲开本机交易中心 5–7s 代码 tick 尖峰；连续竞价正常约 50ms 一批）。
- **窗口外**（含午休、15:00 后）：保持订阅，**禁止**因静默而重订。
- 09:25–09:30 可能偏静，15s 误触一次重订可接受。
- 成功重订后把 `lastPushTime` 设为当前时间，避免立刻再拆（等效 15s 宽限）。不另设 60s 冷却。

判定必须同时成立：`ping` 成功，且距 `lastPushTime` ≥ 15s。`ping` 走现有 `BigQmtRpcClient` 超时；失败只记 `PING_FAIL`，不重订。启动后尚未收到任何增量时，`lastPushTime` 视为订阅成功时刻（避免一启动就重订）。

## 桥接改动（`D:\cursor\xtquant_big_convert`）

同步到 `D:\银河证券QMT实盘 - 交易终端-上海\python\bigqmt_signal_trader\`，然后**重启策略「大QMT桥接」**。

| 文件 | 行为 |
|---|---|
| `quote_push_channel.py` | `publish` 按代码合并最新瘦字段；专线程 `send_multipart`。发送异常只打日志，不抛回行情回调。 |
| `quote_subscription_manager.py` | `on_push` 仍只调 `publisher(topic, data)`，行情线程不做 encode。 |
| `whole_quote_session.py` | 默认 `push_silence_replay_heartbeats=0`，关闭静默幂等 replay。 |
| `xtquant_compat.py` | 大列表继续默认 `prime=False`；会话使用上述默认。 |

不改：下单开关、`rt_k`、opt-t-board、`大QMT桥接.py` 业务逻辑。跨线程仍禁止在回调里 close ZMQ socket。

## 脚本行为

- 路径：仓库根目录 `大QMT实时行情.py`；日志 `大QMT实时行情.log`（按日轮转、保留 3 天、Windows 安全 Handler）。
- 宇宙：`quote_universe.build_quote_universe()`（沪深 A + 京市 A 股 + 沪深 ETF，债不订）。
- `subscribe_whole_quote(codes, callback=..., prime=False)`。禁止 `get_full_tick` 大列表。
- 回调：更新批次数、更新条数、unique、`lastPushTime`、可选五档样本计数；不写 MySQL。
- 心跳约每 60s 一行：批次、unique、最大间隔、`ping_ms`、重订次数、`lastPush`。
- Ctrl+C / SIGTERM：`running=False`，退订，停 session，退出。
- 本脚本运行期间不要并行 `quote-live` 订同一 7214 combo，否则本脚本退订不会拆掉 QMT 句柄（引用计数仍 >0）。

### 重订

1. `unsubscribe_whole_quote` **必须成功**，否则本轮放弃再订，记 `RESUB_FAIL`。
2. 再 `subscribe_whole_quote(..., prime=False)`，成功记 `RESUB_OK`。
3. 连续 3 次 `RESUB_FAIL`：进入放弃态，每 60s 打 `RESUB_GIVE_UP`；**冷却 60s 后自动清失败计数再试**（`RESUB_RETRY`），不需人工重启。窗口内若又收到增量，立即退出放弃态。

## 测试

**实施后、交易日前（本机）：**

- 桥：队列满丢最旧；`publish` 立即返回、不在调用线程 encode/send（mock PUB）。
- 脚本：窗口函数；15s 判定；ping 失败不重订；退订失败不订。假时钟，不连 QMT。
- 审查脚本：无写库、无大列表 `get_full_tick`、`prime=False`。

**交易日（用户）：**

- 09:15 后有增量；午休无 `RESUB_OK`；连续竞价静默 15s 应 `RESUB_OK` 且批次数继续增加。
- 禁止用 `quote-prime` 或 6876 打底验证。

## 成功标准

交易时段内脚本保持运行；假活后在 15s 量级自愈，不再出现「ping 已通、批次数钉死直到收盘」。不要求消灭 QMT 内核偶发数分钟卡死，只要求卡完后能重建订阅。

## 已知未修（2026-08-24）

全推发送队列溢流：**代码已改**（合并最新 + 瘦字段，已拷进 QMT），待重启「大QMT桥接」后下一交易日验收。记录见 [daqmt-publish-queue-overflow-20260824.md](../../solutions/performance-issues/daqmt-publish-queue-overflow-20260824.md)。
