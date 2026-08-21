# 大 QMT 全推长稳保活 Implementation Plan

> **For agentic workers:** 本会话按用户「请执行」在当前对话内联实施。桥接改 `xtquant_big_convert`（该仓库有大量无关脏文件，**不整库提交**）；脚本与测试提交到 `tushareindex`。

**Goal:** QMT 全推回调入队发送；`大QMT实时行情.py` 在交易窗口内 15s 假活且 ping 通则真退真订；不写库。

**Architecture:** 桥接 `ZmqQuotePushChannel.publish` 只入有界队列，专线程 encode/send。客户端 `WholeQuoteClientSession` 默认关闭静默幂等 replay。常驻脚本持有交易日历与重建逻辑。

**Tech Stack:** Python 3.12、ZMQ、`bigqmt_signal_trader`、unittest/pytest。

## Global Constraints

- 不上 Redis；不替换 `rt_k`；禁止大列表 `get_full_tick` / `prime=True`。
- 假活窗口（本地时区，半开）：`[09:15, 11:30)`、`[13:00, 15:00)`；N=15s。
- 队列深度 256，满丢最旧；退订 RPC 失败则本轮不订。
- 标识符 camelCase；脚本单文件；日志与 py 同名、轮转 3 天。
- 同步 `quote_push_channel.py` 到银河 QMT `python\bigqmt_signal_trader\` 后须重启「大QMT桥接」。

---

### Task 1: ZMQ PUB 有界队列

**Files:**
- Modify: `D:/cursor/xtquant_big_convert/src/bigqmt_signal_trader/quote_push_channel.py`
- Test: `D:/cursor/xtquant_big_convert/tests/bigqmt_signal_trader/test_quote_push_channel.py`
- Sync: `D:/银河证券QMT实盘 - 交易终端-上海/python/bigqmt_signal_trader/quote_push_channel.py`

- [x] 队列满丢最旧；`publish` 不在调用线程 encode
- [x] 现有 pub/sub 单测仍过
- [x] 同步 QMT 副本

### Task 2: 关闭客户端静默幂等 replay

**Files:**
- Modify: `D:/cursor/xtquant_big_convert/src/bigqmt_signal_trader/whole_quote_session.py`
- Test: `D:/cursor/xtquant_big_convert/tests/bigqmt_signal_trader/test_whole_quote_client.py`

- [x] 默认 `push_silence_replay_heartbeats=0` 且 `>0` 才 replay（避免 `>=0` 每轮重放）
- [x] 显式阈值的旧单测仍过

### Task 3: 常驻脚本与看门狗单测

**Files:**
- Create: `d:/cursor/tushareindex/大QMT实时行情.py`
- Create: `d:/cursor/tushareindex/tests/test_daqmt_realtime_quote.py`

- [x] 窗口 / 15s / ping 失败不订 / 退订失败不订 / 连续 3 次放弃
- [x] `prime=False`、无写库、无 `get_full_tick`
- [x] 审查脚本后反馈

### Task 4: 用户交易日验证（不在本会话）

- 重启「大QMT桥接」后运行 `python 大QMT实时行情.py`
- 禁止 `quote-prime` / 6876 打底
