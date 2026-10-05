# coding: utf-8
"""大 QMT 全推长稳保活：订 7214 增量，窗口内假活则真退真订。

期权（2026-08-31 配额按标的切开）：
- 全推 subscribe_whole_quote 不含 .SHO/.SZO：ETF 走全推，期权 0 次。
- subscribe_quote(tick) 只订 588000+510500+510300 全链（约 418≤500）；
  510050 整链走 get_option_ticks 轮询。深市不订（实测 0 回调）。
  159915 + 510050 可由通达信独立进程（tdx_opt_live.py :15581）主供；
  510300/510500/588000 可由同一进程慢刷兜底（缺档/盘后盘口）。
  本进程只 HTTP 合并；通达信挂了仍走 1m / 50ETF 轮询兜底。禁止在本进程
  import tqcenter。
- 直播窗：缓存命中不打 tick；无盘口才 get_option_ticks；已有盘口墙钟过期则
  小片增量灌今日 tick 再回读。禁止整链反复读冻 DAT 把 opt_last 刷成假活。
  过期刷新：有量优先 + 轮转扫尾；全推站住 40 张/轮，否则 20。
  全推静默≥8s / ping BUSY / 重订占锁 → 停期权重 RPC，把 GIL 还给回调。
  期权 tick 超时连败则退避 30s，同因 WARNING 60s 限频（后盘 catch-up 同此）。
- 创业板不用五档：直播窗本地 1m；柱过期才小片增量灌 1m（已有盘口约 30s 一灌）。让路只在全推真死时。

本地 datadir 直读（2026-08-28 实测，勿误解为「不用 RPC 就能实时」）：
- 推送数据永不落盘：沪市订着 tick 的合约盘中 RPC 能读到实时价，但 SHO/0/<code>/ 下
  当日 .dat 根本不存在；当日 tick 文件数 SHO=0（订 500 张未 download）、
  SZO=132（未订但 download 了 132 张），落盘只由 download 触发。
- 故 download 那步 RPC 省不掉；能省的是 download **之后**再用 RPC 把数据传回来。
  1m/1d 取回已改为直读 <datadir>/{市场}/{60|86400}/{代码}.DAT，缺口才按合约回退 RPC。
- 格式：定长 64 字节小端、自 offset 0 起，多余 8 字节在文件**尾部**（15432=241×64+8）。

拉链未增长则 90s 重试。假活：ping BUSY 或 ping 慢时先让期权让路再 ping，
快且静默才真退真订（force 拆 combo）。窗口内 unique=0 / 静默≥8s 暂停
期权重 RPC，五只 ETF get_full_tick 探针续盘口（禁止市场码）。
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta, time as dtTime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import TimedRotatingFileHandler

# 期权 RPC 会饿死全推回调；15s 会把「自堵」误判成假活去真退真订。
SILENCE_SECONDS = 25.0
# 订完后给第一批增量的宽限；未推过时静默从窗口起点算，避免一订完就拆。
SUBSCRIBE_GRACE_SEC = 20.0
# 真订 7214 约 0.8~1.3s；挂到已有死 combo 只需 ~50ms。
SUBSCRIBE_FAST_SEC = 0.25
ETF_PROBE_LIVE_SEC = 3.0
ETF_PROBE_IDLE_SEC = 30.0
WATCHDOG_SLEEP_SEC = 1.0
HEARTBEAT_SEC = 60.0
# 总退出等待包含正在执行的 RPC、共享锁和清理；不能只依赖 RPC 自身超时。
SHUTDOWN_TIMEOUT_SEC = 5.0
RESUB_FAIL_MAX = 3
GIVE_UP_LOG_SEC = 60.0
GIVE_UP_RETRY_SEC = 60.0
RESUB_CONFIRM_SEC = 15.0
# 期权分片 RPC 会占满 ROUTER：单片最长 OPT_RPC_TIMEOUT_SEC；ping 须大于该值。
PING_WATCHDOG_TIMEOUT_SEC = 12.0
# ping>该值是 ROUTER 忙，不是全推假活；再订 7214 会更堵。
PING_FAKE_ALIVE_MAX_MS = 500.0
# 全推增量「从未真正进缓存」的判据：unique 低于该值即视为 combo 未订上。
# 与 ping 快慢解耦——桥满载时 ping 3~6s 会让旧的纯 ping 判据永久放弃自救。
QUOTE_CRITICAL_UNIQUE_N = int(os.getenv("DAQMT_CRITICAL_UNIQUE_N", "50"))
# ping 连续 busy 达该次数且全推仍静默 → 强制重订（不无限让路）。
QUOTE_BUSY_RESUB_STREAK = int(os.getenv("DAQMT_BUSY_RESUB_STREAK", "3"))
# QMT 主导链的「让位」阈值：当前 QMT tick 超过该秒数未更新，就让 TDX 接管盘口。
# 实测 2026-09-11 下午盘 QMT 期权源卡死 155s+，而 TDX 仅 2~35s。
# 必须明显小于 OPT_QMT_TICK_STALE_SEC，才能及时降级。
QMT_LED_MAX_STALE_SEC = float(os.getenv("DAQMT_QMT_LED_MAX_STALE_SEC", "30"))
# QMT get_option_ticks 冻结判据：进来的 tick 与记录盘口时间差超过该秒数即视为陈旧，
# 不得用其 last/vol 回盖 TDX 新鲜值（持仓量仍允许补）。
OPT_QMT_TICK_STALE_SEC = float(os.getenv("DAQMT_QMT_TICK_STALE_SEC", "90"))
# 期权日线取回的字段：必须显式给，空表会走 RPC 全列慢路径并打满桥队列。
OPT_DAILY_FIELDS = ["open", "high", "low", "close", "volume", "openInterest"]
RPC_LOCK_PING_WAIT_SEC = 0.15
SNAP_JSON_TTL_SEC = 0.4
INSTANCE_LOCK_NAME = "daqmt_rt_quote.lock"
SAMPLE_CODES = ("588000.SH", "510300.SH", "000001.SZ")
XTQUANT_SRC = r"D:\cursor\xtquant_big_convert\src"
OPT_UNDERLYINGS = ("510050", "510300", "510500", "588000", "159915")
OPT_UNDERLYING_QMT = (
    "510050.SH",
    "510300.SH",
    "510500.SH",
    "588000.SH",
    "159915.SZ",
)
# 行情来源分工（2026-09-11 与用户确认）：
#   510300 / 510500 / 588000 → QMT（subscribe_quote 订阅配额约 418≤500）
#   510050 / 159915          → 通达信 tdx_opt_live.py(:15581)
#                              （510050 不在订阅配额内；159915 深市无 tick 回调）
# 因此 QMT 的期权 tick 宇宙 / 拉链 / 快照只覆盖前三条链；510050 不进 QMT 期权管线，
# 其 ETF 现货仍由全推供给（OPT_UNDERLYING_QMT 保留五只，用于现货与探针）。
OPT_QMT_TICK_UNDERLYINGS = ("510300", "510500", "588000")
# 桥接 get_market_data_ex 单批约 100；50/8s 仍常超时。沪市 tick 片 20。
OPT_CHUNK_SIZE = 20
# 片间让出给全推回调 / ping；50ms 不够一次 PUB 排水。
OPT_CHUNK_YIELD_SEC = 0.4
# 超时对半重试：直播窗片宽仅 20，旧阈值 80 导致从不对拆、整片白烧 5s。
OPT_CHUNK_SPLIT_MIN = 8
OPT_CHUNK_SPLIT_MAX_DEPTH = 2
OPT_RPC_TIMEOUT_SEC = 5.0
OPT_POLL_FAIL_ABORT = 2
# tick 整轮失败后歇这么久，避免 0.4s 连打把 ROUTER 和日志一起打满。
OPT_FAIL_BACKOFF_SEC = 30.0
OPT_WARN_COOLDOWN_SEC = 60.0
# 午间 ROUTER 忙：连续失败后临时缩小片宽/轮询张数，给全推让路。
OPT_CHUNK_PRESSURE_SEC = float(os.getenv("DAQMT_OPT_CHUNK_PRESSURE_SEC", "90"))
OPT_CHUNK_PRESSURE_SIZE = int(os.getenv("DAQMT_OPT_CHUNK_PRESSURE_SIZE", "10"))
OPT_CHUNK_PRESSURE_MAX_CHUNKS = int(os.getenv("DAQMT_OPT_CHUNK_PRESSURE_MAX_CHUNKS", "1"))
OPT_CHUNK_PRESSURE_YIELD_SEC = float(os.getenv("DAQMT_OPT_CHUNK_PRESSURE_YIELD_SEC", "0.8"))
OPT_CHUNK_PRESSURE_TIMEOUT_SEC = float(os.getenv("DAQMT_OPT_CHUNK_PRESSURE_TIMEOUT_SEC", "4"))
# OI 压力时让非关键 OPT_CHUNK 让出共享 QMT RPC。只做有界跳过，不提高并发/批量；
# 默认开启，设为0并重启即可立即回退到旧调度。
OPT_CHUNK_OI_GATE_ENABLE = os.getenv(
    "DAQMT_OPT_CHUNK_OI_GATE", "1"
).strip().lower() not in {"0", "false", "no", "off"}
OPT_CHUNK_OI_GATE_MIN_DEMAND_N = max(
    1, int(os.getenv("DAQMT_OPT_CHUNK_OI_GATE_MIN_DEMAND_N", "6"))
)
OPT_CHUNK_OI_GATE_WAIT_P95_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_CHUNK_OI_GATE_WAIT_P95_SEC", "15"))
)
OPT_CHUNK_OI_GATE_P0_MAX_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_CHUNK_OI_GATE_P0_MAX_SEC", "30"))
)
OPT_CHUNK_OI_GATE_GAP_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_CHUNK_OI_GATE_GAP_SEC", "12"))
)
OPT_CHUNK_OI_GATE_STATE_MAX_AGE_SEC = max(
    1.0, float(os.getenv("DAQMT_OPT_CHUNK_OI_GATE_STATE_MAX_AGE_SEC", "60"))
)
OPT_CHUNK_OI_GATE_PROBE_INTERVAL_SEC = max(
    1.0, float(os.getenv("DAQMT_OPT_CHUNK_OI_GATE_PROBE_INTERVAL_SEC", "15"))
)
OPT_CHUNK_OI_GATE_LOG_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_CHUNK_OI_GATE_LOG_SEC", "15"))
)
OPT_SHO_POLL_MAX_CHUNKS = 4
# 直播窗冷缺口才轮询；过期盘口走增量灌库，不再每轮扫 80 张冻 DAT。
OPT_LIVE_TICK_MAX_CHUNKS = 2
# 过期刷新每轮张数；冷启动灌库仍用 OPT_LIVE_DL_CHUNK。
OPT_LIVE_REFRESH_CHUNK = 20
OPT_LIVE_REFRESH_CHUNK_FAST = 40
OPT_LIVE_REFRESH_FAST_UNIQUE_MIN = 7000
OPT_LIVE_REFRESH_FAST_SILENCE_MAX = 1.0
OPT_LIVE_REFRESH_COOLDOWN_SEC = 30.0
# 40 张里约 30 张有量优先，其余轮转扫尾，避免远月永远不刷。
OPT_LIVE_REFRESH_PRIORITY_RATIO = 0.75
# 内存盘口墙钟超过该秒数视为过期，触发增量灌库。
OPT_LIVE_TICK_STALE_SEC = 60.0
OPT_YIELD_SILENCE_SEC = 8.0
OPT_YIELD_REST_SEC = 3.0
OPT_YIELD_LOG_SEC = 15.0
# QMT 全推静默时，OPT_QTY 的 get_market_data_ex 会与回调争用同一策略线程。
# 此时先让订阅恢复；OI 保留上一笔真实时钟并在全推健康后按 P0/P1 补齐。
OPT_QTY_WHEN_QUOTE_DEAD = False
# 510050 / 159915 的盘口由 TDX 主供，QMT 仅补最新/量/仓；因此 OI 轮询必须
# 优先服务这两条链，不能与三条已订阅 QMT 主链一起做 650 张盲扫。
OPT_QTY_TDX_UNDLS = ("510050", "159915")
# OI 只能在同一个 QMT 策略线程中串行请求。实测两批 20 张会持续占用 8--14 秒，
# 因而绝不恢复该模式。先以 6 张守住 P0，再在全推健康时单请求探测 8/10 张；
# 一次超时、让位或 P0 不足即回退。P0 基线为 TDX 两链每链 3 张。
OPT_QTY_MODE_SPECS = (
    {"name": "base6", "chunkSize": 6, "maxChunks": 1, "p0Slots": 6, "tailSlots": 0, "maxRpcSec": 3.0},
    {"name": "probe8", "chunkSize": 8, "maxChunks": 1, "p0Slots": 6, "tailSlots": 2, "maxRpcSec": 3.5},
    {"name": "probe10", "chunkSize": 10, "maxChunks": 1, "p0Slots": 6, "tailSlots": 4, "maxRpcSec": 4.0},
)
# P0 仍按既有10秒SLA优先轮转；仅把当轮不需要的保留槽借给最老长尾。
# 关闭后精确恢复固定 p0Slots/tailSlots 配额，便于盘中秒退。
OPT_QTY_DYNAMIC_P0_LOAN_ENABLE = os.getenv(
    "DAQMT_OPT_QTY_DYNAMIC_P0_LOAN", "1"
).strip().lower() not in {"0", "false", "no", "off"}
OPT_QTY_P0_LOAN_GUARD = max(
    1.0, float(os.getenv("DAQMT_OPT_QTY_P0_LOAN_GUARD", "1.25"))
)
# 有界借槽的独立安全门。旧动态借槽开关仍保留，关闭任一开关都立即回到固定配额。
# 默认只借1槽；第二槽必须显式打开且使用更严阈值，避免盘中需求集合持续变动时
# 为了长尾而牺牲 P0 OI 的新鲜度。
OPT_QTY_BOUNDED_P0_LOAN_ENABLE = os.getenv(
    "DAQMT_OPT_QTY_BOUNDED_P0_LOAN", "1"
).strip().lower() not in {"0", "false", "no", "off"}
OPT_QTY_P0_LOAN_MAX_SLOTS = max(
    0, min(4, int(os.getenv("DAQMT_OPT_QTY_P0_LOAN_MAX_SLOTS", "1")))
)
OPT_QTY_P0_LOAN_ALLOW_TWO = os.getenv(
    "DAQMT_OPT_QTY_P0_LOAN_ALLOW_TWO", "0"
).strip().lower() not in {"0", "false", "no", "off"}
OPT_QTY_P0_LOAN_MAX_DEMAND_N = max(
    0, int(os.getenv("DAQMT_OPT_QTY_P0_LOAN_MAX_DEMAND_N", "24"))
)
OPT_QTY_P0_LOAN_MAX_WAIT_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_P0_LOAN_MAX_WAIT_SEC", "30"))
)
OPT_QTY_P0_LOAN_MAX_OI_P95_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_P0_LOAN_MAX_OI_P95_SEC", "30"))
)
OPT_QTY_P0_LOAN_MAX_OI_MAX_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_P0_LOAN_MAX_OI_MAX_SEC", "45"))
)
OPT_QTY_P0_LOAN_SECOND_MAX_DEMAND_N = max(
    0, int(os.getenv("DAQMT_OPT_QTY_P0_LOAN_SECOND_MAX_DEMAND_N", "8"))
)
OPT_QTY_P0_LOAN_SECOND_MAX_WAIT_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_P0_LOAN_SECOND_MAX_WAIT_SEC", "10"))
)
OPT_QTY_P0_LOAN_SECOND_MAX_OI_P95_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_P0_LOAN_SECOND_MAX_OI_P95_SEC", "15"))
)
OPT_QTY_P0_LOAN_SECOND_MAX_OI_MAX_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_P0_LOAN_SECOND_MAX_OI_MAX_SEC", "20"))
)
OPT_QTY_PROMOTE_GOOD_ROUNDS = int(os.getenv("DAQMT_OPT_QTY_PROMOTE_GOOD_ROUNDS", "3"))
OPT_QTY_POSITION_TTL_SEC = float(os.getenv("DAQMT_OPT_QTY_POSITION_TTL_SEC", "15"))
OPT_QTY_POSITION_QUERY_TIMEOUT_SEC = max(
    1, int(os.getenv("DAQMT_OPT_QTY_POSITION_QUERY_TIMEOUT_SEC", "2"))
)
OPT_QTY_P0_TARGET_SEC = float(os.getenv("DAQMT_OPT_QTY_P0_TARGET_SEC", "10"))
OPT_QTY_P1_TARGET_SEC = float(os.getenv("DAQMT_OPT_QTY_P1_TARGET_SEC", "45"))
OPT_QTY_P1_MAX_SEC = float(os.getenv("DAQMT_OPT_QTY_P1_MAX_SEC", "60"))
OPT_QTY_PRIORITY_PER_UNDL = int(os.getenv("DAQMT_OPT_QTY_PRIORITY_PER_UNDL", "3"))
# 长尾不改变批量/并发，只改变同一到期月内的顺序：OI 缺失或最老者先轮转。
# 设为0可立即恢复旧的成交量/持仓量排序，便于盘中回退。
OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE = os.getenv(
    "DAQMT_OPT_QTY_TAIL_OI_AGE_PRIORITY", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# 只读诊断：分解 P0 的真实来源与进程内连续等待年龄。默认30秒一条，设0关闭日志；
# 诊断仍不参与计划器输入，不能改变批量、并发、游标或数据源行为。
OPT_QTY_P0_DIAG_LOG_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_P0_DIAG_LOG_SEC", "30"))
)
# 只读诊断：比较长尾全集与本轮实际入选合约的 OI 年龄；不参与排序、游标或
# 配额。设为0关闭日志，后续任何候选顺序调整都必须先由这组运行态证据支持。
OPT_QTY_TAIL_DIAG_LOG_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QTY_TAIL_DIAG_LOG_SEC", "30"))
)
# 只读诊断：拆解 SZO 分钟 OI 卸载的每一道资格门控；不参与计划器输入。
OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC", "30"))
)
# QMT 主导三链通常由 subscribe_quote 提供 OI；仅当该字段超时/缺失时，才把
# 每链最重要的少量合约临时加入 QMT 轮询修复 P0。不能把整条 QMT 链重新塞回
# 650 张 OI 轮转，否则会挤坏 510050/159915 的既有公平轮转。
OPT_QTY_QMT_REPAIR_PER_UNDL = int(os.getenv("DAQMT_OPT_QTY_QMT_REPAIR_PER_UNDL", "4"))
OPT_QTY_QMT_OI_REPAIR_STALE_SEC = float(
    os.getenv("DAQMT_OPT_QTY_QMT_OI_REPAIR_STALE_SEC", "45")
)
# 午休只保留收盘后短暂量仓收尾；之后释放 QMT 策略线程，避免拖到 13:00。
OPT_LUNCH_HEAVY_GRACE_SEC = max(
    0.0, min(1800.0, float(os.getenv("DAQMT_OPT_LUNCH_HEAVY_GRACE_SEC", "300")))
)
OPT_IDLE_SLEEP_SEC = 0.3
OPT_DAILY_IDLE_SEC = 10.0
# 直播窗 round 结束后至少歇这么久，避免 tick 失败时 0.2s 连打 1d。
OPT_DAILY_LIVE_SEC = 10.0
OPT_LIVE_ROUND_REST_SEC = 0.4
# 直播窗主源是 tick；1d 备份拉稀，避免每轮 640 张日 K 再堵 13s。
OPT_DAILY_WITH_TICK_SEC = 90.0
OPT_TICK_FRESH_SEC = 180.0
OPT_DAILY_RPC_TIMEOUT_SEC = 12.0
OPT_CHAIN_RPC_TIMEOUT_SEC = 3.0
OPT_CHAIN_MIN_PER = 40
OPT_CHAIN_MIN_TOTAL = 500
OPT_CHAIN_FAIL_BACKOFF_SEC = 60.0
OPT_CHAIN_ZIP_DELAY_SEC = 4.0
OPT_CHAIN_ZIP_MAX_FAIL = 3
OPT_CHAIN_INCOMPLETE_RETRY_SEC = 90.0
OPT_CHAIN_CONFIRM_SEC = 600.0
OPT_CHAIN_SETTLE_HITS = 2
OPT_LISTED_LOOKBACK_TRADE_DAYS = 4
OPT_LISTED_LOOKBACK_CAL_FALLBACK_DAYS = 10
OPT_META_RETRY_SEC = 60.0
OPT_META_BATCH = 20
OPT_META_LIVE_GAP_BATCH = 8
OPT_META_YIELD_SEC = 0.05
# 期权 tick 不进全推；QMT 本地库过期时 get_option_ticks 会冻在旧日。
# 先 RPC download_history_data2(period=tick) 灌库，再轮询。SZO（创业板）优先。
OPT_TICK_DL_CHUNK = 15
# 直播窗灌库：全推健康后才小片拉今日 tick/1m，避免再堵死 7214。
OPT_LIVE_DL_CHUNK = 40
OPT_LIVE_DL_TIMEOUT_SEC = 30.0
OPT_LIVE_DL_UNIQUE_MIN = 1000
OPT_QUOTE_CACHE_RETRY_SEC = 20.0
# 直播窗新月上限：超过视为重启后空 tick 缓存，禁止整链灌库。
OPT_TICK_GAP_MAX_N = 120
OPT_TICK_DL_TIMEOUT_SEC = 120.0
OPT_TICK_DL_LOOKBACK_DAYS = 7
OPT_DAILY_DL_CHUNK = 200
OPT_DAILY_DL_TIMEOUT_SEC = 45.0
# 订阅失败时才灌创业板当日 1d；成功则不再 90s 灌库。
OPT_SZO_DAILY_DL_SEC = 90.0
# 今日新挂本地 1d 会冻在早盘快照（有量也不跟盘），须定期再灌，不能走 stub 一次过滤。
OPT_LISTED_DAILY_DL_SEC = 60.0
OPT_LISTED_DAILY_DL_CHUNK = 40
# OPT_SZO_SUB_* 是历史前缀，现用于沪市 subscribe_quote（深市已不订）。
OPT_SZO_SUB_PERIOD = "tick"
# 科创+500+300 约 418 张：40/轮约 11 轮订满。实测每轮 RPC 约 0.1s/张。
OPT_SZO_SUB_ADD_LIMIT = 40
OPT_SZO_SUB_TIMEOUT_SEC = 20.0
OPT_SZO_SUB_YIELD_SEC = 0.4
OPT_SZO_SUB_RETRY_SEC = 15.0
# InnerAPI 品种+周期 500：科创+中证500+沪深300 全订（约 418），50ETF 全轮询。
# 深市不订：实测 SZO 订满 132 张仍 0 回调，只会占配额把沪市挤成轮询。
OPT_QUOTE_SUB_MAX_OWNED = 500
OPT_QUOTE_SUB_UNDLS = ("588000", "510500", "510300")
# QMT 主导链：这些标的的 SHO 盘口不走通达信覆盖（TDX 仅作 QMT 不可用时的兜底）。
# 约定：510500/510300/588000 走 QMT（订阅配额 418≤500），510050/159915 走通达信。
QMT_LED_ENABLE = os.getenv("DAQMT_QMT_LED_ENABLE", "1") != "0"
QMT_LED_UNDLS = frozenset(
    x.strip()
    for x in os.getenv(
        "DAQMT_QMT_LED_UNDLS", ",".join(OPT_QUOTE_SUB_UNDLS)
    ).split(",")
    if x.strip()
)
OPT_QUOTE_POLL_UNDLS = ("510050",)
OPT_QUOTE_CACHE_MAX_AGE_SEC = 30.0
# 后盘重启：回调停了，缓存里仍是收盘 tick，年龄按小时计。
OPT_QUOTE_CACHE_CATCHUP_AGE_SEC = 28800.0
OPT_CATCHUP_CHUNKS = 8
# 盘中回调缓存连续空 N 轮：订满仍无推送则 force 分片重订（含从未 hit）。
OPT_QUOTE_CACHE_EMPTY_RESYNC = 3
# 缓存读失败/桥忙不是权威空缓存；关闭开关可立即恢复旧判据。
OPT_QUOTE_CACHE_GUARD_ENABLE = os.getenv(
    "DAQMT_OPT_QUOTE_CACHE_GUARD", "1"
) not in ("0", "false", "False")
OPT_QUOTE_CACHE_POST_QTY_GRACE_SEC = max(
    0.0, float(os.getenv("DAQMT_OPT_QUOTE_CACHE_POST_QTY_GRACE_SEC", "2"))
)
# 订满后仍定期 sync，让桥接按 cache 静默自动 force；勿只在未订满时才 sync。
OPT_QUOTE_SUB_HEAL_SEC = 90.0
# 同一批未进缓存连续 heal 这么多次仍不涨，改 poll-only，停止空退空订。
OPT_QUOTE_HEAL_FAIL_STOP = 3
# 订阅推送「退化」判据（2026-09-11）：命中率低于该比例且连续 N 次 → 视为源侧退化，
# 触发 force 重订。现场：午休→下午盘边界后 hit 由 ~140 掉到 16~48（约 1/6），
# 持续约 10 分钟，而旧判据只认 hit==0，因此从不重订。
OPT_SUB_DEGRADE_RATIO = float(os.getenv("DAQMT_OPT_SUB_DEGRADE_RATIO", "0.15"))
OPT_SUB_DEGRADE_STREAK = int(os.getenv("DAQMT_OPT_SUB_DEGRADE_STREAK", "3"))
# 午休空缓存是常态，禁止 3 轮就 force；连续竞价里两次 force 至少隔这么久。
OPT_QUOTE_SUB_FORCE_COOLDOWN_SEC = 90.0
# 一次受控 force 后给 QMT callback 一个短确认窗；仍无新事件才熔断反复退订/重订。
OPT_QUOTE_SUB_FORCE_CONFIRM_SEC = float(
    os.getenv("DAQMT_OPT_SUB_FORCE_CONFIRM_SEC", "20")
)
# force 后偶发的一帧 callback 不能立即解除熔断；必须持续新鲜一个确认窗。
OPT_QUOTE_SUB_RECOVER_CONFIRM_SEC = float(
    os.getenv("DAQMT_OPT_SUB_RECOVER_CONFIRM_SEC", "20")
)
# 与桥接 redis_rpc.RPC_REVISION 对齐；对不上则拒绝期权 subscribe_quote。
EXPECTED_RPC_REVISION = os.getenv("DAQMT_EXPECT_RPC_REVISION", "20260903-opt-heal-v2")
OPT_HEALTH_DEAD_PUSH_LOG_SEC = 60.0
# 创业板不用五档：直播窗只读本地 1m DAT；灌库仅后盘。
OPT_SZO_MIN_PERIOD = "1m"
OPT_SZO_MIN_BAR_COUNT = 240
OPT_SZO_MIN_READ_CHUNK = 20
OPT_SZO_MIN_DL_CHUNK = 40
OPT_SZO_MIN_DL_TIMEOUT_SEC = 45.0
OPT_SZO_MIN_DL_CHUNK_TIMEOUT_SEC = 20.0
# 新分钟开始后再等这么久灌 1m，给文件落盘；不要用墙钟 25s 在同一根分钟线内空转。
OPT_SZO_MIN_STALE_SEC = 8.0
OPT_SZO_MIN_FAIL_BACKOFF_SEC = 90.0
# 本地 1m OI 先保留影子统计；严格更新且带独立 oiTime 时才允许仅字段覆盖。
OPT_SZO_OI_SHADOW_ENABLE = os.getenv("DAQMT_OPT_SZO_OI_SHADOW", "1") != "0"
OPT_SZO_OI_SHADOW_REPORT_SEC = max(
    10.0, float(os.getenv("DAQMT_OPT_SZO_OI_SHADOW_REPORT_SEC", "60"))
)
# 分钟 OI 与报价有效性解耦：零成交柱仍可携带交易所持仓量。设 0 可秒退旧行为。
OPT_MINUTE_OI_ONLY_ENABLE = os.getenv(
    "DAQMT_OPT_MINUTE_OI_ONLY", "1"
) not in ("0", "false", "False")
# 510050 长尾 OI：QMT 增量下载 1m 后直读 SHO DAT。关键合约仍由 tick OI 服务。
# 快速回退：环境变量设 0 后重启，或运行中创建 disable flag（下一轮即停，无需重启）。
OPT_SHO_MIN_OI_ENABLE = os.getenv("DAQMT_OPT_SHO_MIN_OI", "1") != "0"
OPT_SHO_MIN_OI_DISABLE_FILE = os.getenv(
    "DAQMT_OPT_SHO_MIN_OI_DISABLE_FILE",
    os.path.join(os.environ.get("TEMP") or ".", "daqmt_disable_510050_minute_oi.flag"),
)
OPT_SHO_MIN_OI_CHUNK = max(
    1, min(10, int(os.getenv("DAQMT_OPT_SHO_MIN_OI_CHUNK", "10")))
)
OPT_SHO_MIN_OI_INTERVAL_SEC = max(
    5.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_INTERVAL_SEC", "8"))
)
OPT_SHO_MIN_OI_CODE_COOLDOWN_SEC = max(
    30.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_CODE_COOLDOWN_SEC", "45"))
)
OPT_SHO_MIN_OI_UNAVAILABLE_COOLDOWN_SEC = max(
    60.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_UNAVAILABLE_COOLDOWN_SEC", "600"))
)
OPT_SHO_MIN_OI_KNOWN_SLOTS = max(
    1, min(OPT_SHO_MIN_OI_CHUNK, int(os.getenv("DAQMT_OPT_SHO_MIN_OI_KNOWN_SLOTS", "8")))
)
OPT_SHO_MIN_OI_PROBE_SLOTS = max(
    0, min(OPT_SHO_MIN_OI_CHUNK, int(os.getenv("DAQMT_OPT_SHO_MIN_OI_PROBE_SLOTS", "2")))
)
OPT_SHO_MIN_OI_RPC_TIMEOUT_SEC = max(
    2.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_RPC_TIMEOUT_SEC", "4"))
)
OPT_SHO_MIN_OI_MAX_SUBMIT_SEC = max(
    1.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_MAX_SUBMIT_SEC", "3"))
)
OPT_SHO_MIN_OI_MAX_OBS_AGE_SEC = max(
    60.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_MAX_OBS_AGE_SEC", "180"))
)
OPT_SHO_MIN_OI_CIRCUIT_SEC = max(
    60.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_CIRCUIT_SEC", "300"))
)
OPT_SHO_MIN_OI_ACTIVE_TTL_SEC = max(
    30.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_ACTIVE_TTL_SEC", "90"))
)
# 510050 分钟 OI 的时间租约先只做影子观测，不参与卸载或 QMT 计划。
# 运行态可用环境变量关闭日志；正式门控必须另行 A/B 验证后再接入。
OPT_SHO_MIN_OI_LEASE_SHADOW_ENABLE = os.getenv(
    "DAQMT_OPT_SHO_MIN_OI_LEASE_SHADOW", "1"
) not in ("0", "false", "False", "no", "off")
OPT_SHO_MIN_OI_LEASE_SHADOW_REPORT_SEC = max(
    30.0, float(os.getenv("DAQMT_OPT_SHO_MIN_OI_LEASE_SHADOW_REPORT_SEC", "60"))
)
OPT_SHO_MIN_OI_LEASE_SHADOW_THRESHOLDS = (45.0, 60.0, 75.0, 90.0)
# 严格时间租约的排程影响也仅影子计算。关闭时不构造候选计划，生产计划不受影响。
OPT_SHO_MIN_OI_LEASE_PLAN_SHADOW_ENABLE = os.getenv(
    "DAQMT_OPT_SHO_MIN_OI_LEASE_PLAN_SHADOW", "1"
) not in ("0", "false", "False", "no", "off")
OPT_SHO_MIN_OI_LEASE_PLAN_SHADOW_REPORT_SEC = max(
    30.0,
    float(os.getenv("DAQMT_OPT_SHO_MIN_OI_LEASE_PLAN_SHADOW_REPORT_SEC", "60")),
)
# 三项 OI 调度/观测优化均可独立回退；不改变 QMT 串行模型与单批 10 张上限。
OPT_SHO_MIN_OI_REFILL_ENABLE = os.getenv(
    "DAQMT_OPT_SHO_MIN_OI_REFILL", "1"
) not in ("0", "false", "False")
OPT_SZO_MIN_OI_OFFLOAD_ENABLE = os.getenv(
    # 严格45秒窗口已通过回归与运行态快照验证；可设0立即回退到仅年龄排序。
    "DAQMT_OPT_SZO_MIN_OI_OFFLOAD", "1"
) not in ("0", "false", "False")
# 旧实现按90秒资格卸载会把较旧的分钟 OI 当成更优来源。严格窗口只接管
# 最近一根分钟 OI，超过该年龄自动回到 QMT 长尾；不改变分钟下载频率或批量。
OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC = max(
    30.0,
    float(os.getenv("DAQMT_OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC", "45")),
)
# 有界租约：严格年龄窗之外可通过显式开关在下一分钟下载相位临近时短暂保留
# 本地 OI。盘中 A/B 取证显示宽限会把旧分钟时间戳带入有效 SLA，默认关闭；
# 需要实验时显式设 DAQMT_OPT_SZO_MIN_OI_OFFLOAD_GRACE=1。快照仍透出真实
# oiTime，P0/价量变化仍会强制回到 QMT。
OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE = os.getenv(
    "DAQMT_OPT_SZO_MIN_OI_OFFLOAD_GRACE", "0"
).strip().lower() not in {"0", "false", "no", "off"}
OPT_SZO_MIN_OI_OFFLOAD_GRACE_SEC = max(
    0.0,
    min(60.0, float(os.getenv("DAQMT_OPT_SZO_MIN_OI_OFFLOAD_GRACE_SEC", "30"))),
)
OPT_SZO_MIN_OI_ACTIVE_TTL_SEC = max(
    30.0, float(os.getenv("DAQMT_OPT_SZO_MIN_OI_ACTIVE_TTL_SEC", "90"))
)
OPT_EFFECTIVE_OI_SLA_ENABLE = os.getenv(
    "DAQMT_OPT_EFFECTIVE_OI_SLA", "1"
) not in ("0", "false", "False")
OPT_EFFECTIVE_OI_SLA_SEC = max(
    15.0, float(os.getenv("DAQMT_OPT_EFFECTIVE_OI_SLA_SEC", "60"))
)
# 创业板 1m 已有盘口后不必每轮灌库；无盘口仍立刻补。
OPT_SZO_LIVE_DL_MIN_INTERVAL_SEC = 30.0
# 1m OI 默认每分钟固定在 +8/+38 两个相位尝试，避免滚动 30 秒逐步漂移。
# 盘中 A/B 证实 :23 可改善 159915 OI；不改变每轮 20 张上限或并发，设 0 可立即回退。
OPT_SZO_MIN_PHASE_ALIGN_ENABLE = os.getenv(
    "DAQMT_OPT_SZO_MIN_PHASE_ALIGN", "1"
) not in ("0", "false", "False")
OPT_SZO_MIN_THIRD_PHASE_ENABLE = os.getenv(
    "DAQMT_OPT_SZO_MIN_THIRD_PHASE_ENABLE", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# QMT 1m DAT 的当前未结束柱按分钟结束时刻标记（例如 09:54 期间出现
# 09:55:00）。OI 年龄必须按该柱的分钟起点计算；超出容忍范围的未来时间仍
# fail-closed，避免把本地时钟异常伪装成新鲜行情。设为 0 可立即回退旧口径。
OPT_SZO_MIN_BAR_SECONDS = 60.0
OPT_SZO_MIN_FUTURE_BAR_ALIGN_SEC = max(
    0.0,
    float(os.getenv("DAQMT_OPT_SZO_MIN_FUTURE_BAR_ALIGN_SEC", "90")),
)
OPT_SZO_OI_AGE_SCHED_ENABLE = os.getenv(
    "DAQMT_OPT_SZO_OI_AGE_SCHED", "1"
) not in ("0", "false", "False")
OPT_SZO_OI_REFRESH_STALE_SEC = max(
    30.0, float(os.getenv("DAQMT_OPT_SZO_OI_REFRESH_STALE_SEC", "45"))
)
OPT_SZO_MIN_BLOCK_SHO_N = 20
ETF_TICK_CODE_SET = frozenset(OPT_UNDERLYING_QMT)
# 本地 datadir 直读：download 把数据写进 .DAT 后，直接读文件，省掉再用 RPC 取回的往返。
# 只对「download 之后」的取回有效；推送数据永不落盘，故不能替代订阅。
QMT_DATADIR = os.getenv(
    "QMT_DATADIR", r"D:\银河证券QMT实盘 - 交易终端-上海\datadir"
)
QMT_DAT_ENABLED = os.getenv("QMT_DAT_LOCAL_READ", "1") not in ("0", "false", "False")
# 定长 64 字节小端记录，自 offset 0 起；15432=241×64+8，多余 8 字节在文件尾部。
QMT_DAT_RECORD_SIZE = 64
QMT_DAT_RECORD = struct.Struct("<16I")
QMT_DAT_PRICE_SCALE = 1000.0
# SHO/SZO 权利金 4 位小数；按股票 1000 会把虚值认沽 0.039 读成 0.39。
QMT_DAT_OPT_PRICE_SCALE = 10000.0
QMT_DAT_PERIOD_DIR = {"1m": "60", "1d": "86400"}
# 当日 1m 最多 240 根，取 300 条足够覆盖；日线取 5 条够判当日+昨收。
QMT_DAT_TAIL_MIN = 300
QMT_DAT_TAIL_DAY = 5
QUOTE_HTTP_HOST = "127.0.0.1"
QUOTE_HTTP_PORT = int(os.getenv("DAQMT_QUOTE_HTTP_PORT", "15580"))
# 通达信独立喂数；默认开。关：TDX_OPT_ENABLE=0。勿在本进程加载 tqcenter。
TDX_OPT_ENABLE = os.getenv("TDX_OPT_ENABLE", "1") != "0"
TDX_OPT_SNAP_URL = os.getenv("TDX_OPT_SNAP_URL", "http://127.0.0.1:15581/snapshot")
TDX_OPT_MAX_AGE_SEC = float(os.getenv("TDX_OPT_MAX_AGE_SEC", "8.0"))
# TDX HTTP 合并与重型 QMT RPC 解耦。可设 0 回退到期权轮询线程内的旧路径。
TDX_OPT_DEDICATED_MERGE = os.getenv("TDX_OPT_DEDICATED_MERGE", "1") != "0"
TDX_OPT_MERGE_INTERVAL_SEC = max(
    0.2, float(os.getenv("TDX_OPT_MERGE_INTERVAL_SEC", "1.0"))
)
TDX_OPT_MERGE_REPORT_SEC = max(
    1.0, float(os.getenv("TDX_OPT_MERGE_REPORT_SEC", "10.0"))
)
# 开盘 TQ 忙时 0.8s 易超时；默认 2s，可用 TDX_OPT_FETCH_TIMEOUT_SEC 覆盖
TDX_OPT_FETCH_TIMEOUT_SEC = float(os.getenv("TDX_OPT_FETCH_TIMEOUT_SEC", "2.0"))
# QMT 公式策略桥接离线时，15580 仍用独立 TDX 快照维持五链可读；
# 不能在 waitUntilRpcPing() 前把 TDX 也一起堵死。
TDX_FALLBACK_POLL_SEC = max(
    0.2, float(os.getenv("DAQMT_TDX_FALLBACK_POLL_SEC", "1.0"))
)
TDX_FALLBACK_REPORT_SEC = float(
    os.getenv("DAQMT_TDX_FALLBACK_REPORT_SEC", "30.0")
)
# 主链 + 三链慢刷兜底；可用 TDX_OPT_UNDLS=510050,159915 缩回两链
_TDX_OPT_UNDLS_DEFAULT = "510050,159915,510300,510500,588000"
TDX_OPT_UNDLS = frozenset(
    x.strip()
    for x in os.getenv("TDX_OPT_UNDLS", _TDX_OPT_UNDLS_DEFAULT).split(",")
    if x.strip()
)
QUOTE_RPC_HOST = os.getenv("DAQMT_QUOTE_RPC_HOST", "127.0.0.1")
QUOTE_RPC_PORT = int(os.getenv("DAQMT_QUOTE_RPC_PORT", "15573"))
# 杀旧实例后 ROUTER 需冷却；立刻 ping/订全推会撞上一轮 timeout。
STEAL_SETTLE_SEC = 2.5
# 空订（~50ms）后多歇一会再 force，给桥接拆死 combo。
RESUB_NOOP_SETTLE_SEC = 1.2
SUBSCRIBE_NOOP_RETRY = 3
SUBSCRIBE_NOOP_BACKOFF_SEC = 30.0
# 空订超过此时长仍不放弃会永久挡期权线程 → 持仓/成交量冻死
SUBSCRIBE_NOOP_GIVE_UP_SEC = float(os.getenv("DAQMT_SUBSCRIBE_NOOP_GIVE_UP_SEC", "90"))
INSTANCE_RECHECK_SEC = 60.0
# 冷启动/切源后 ping 常 3~4s；2s 会永远连不上（2026-08-31 实测 3361ms）。
# 启动握手 ping 超时。桥接在高负载下 ping 实测 3.2~3.6s（QMT 公式日志
# zmq slow handler method=ping），6s 会排在其它慢 handler 之后超时，
# 导致启动期误判「桥接不可用」反复重试。放宽到 12s，仅影响启动判定。
RPC_PROBE_TIMEOUT_SEC = float(os.getenv("DAQMT_RPC_PROBE_TIMEOUT", "12.0"))
RPC_WAIT_LOG_SEC = 10.0
RPC_WAIT_SLEEP_SEC = 2.0
SUBSCRIBE_RETRY_SLEEP_SEC = 3.0

running = True
_stopLogged = False
# 同因 WARNING 限频：key → 上次 monotonic。
_optLogLastMono = {}
# OPT_CHUNK 压力窗截止（monotonic）；超时风暴后缩小分片。
_optChunkPressureUntil = 0.0
_qtyPatchRotate = 0
_qtyPatchP0Rotate = 0
_qtyPatchTailRotate = 0
_qtyPatchMode = 0
_qtyPatchGoodRounds = 0
_qtyPatchLastStartMono = 0.0
_qtyPatchRoundN = 0
_qtyPatchPositionCache = (0.0, frozenset())
_qtyP0DiagPendingSinceMs = {}
_qtyP0DiagLastLogMono = 0.0
_qtyTailDiagLastLogMono = 0.0
_szoOiOffloadDiagLastLogMono = 0.0
_qtyP0PressureSnapshot = {
    "observedMono": 0.0,
    "demandN": 0,
    "demandWaitP95Sec": 0.0,
    "p0OiAgeMaxSec": 0.0,
    "gapSec": 0.0,
}
_optChunkOiGateLastActionMono = 0.0
_optChunkOiGateLastLogMono = 0.0
_cacheLock = threading.Lock()
# 期权 tick / 日K / 要素 / ping / 灌库共用 ROUTER，禁止并行打满。
_rpcLock = threading.Lock()
_qtyPressureLock = threading.Lock()
# 全推重订时置位：期权轮询让出 GIL / RPC。
_quoteHold = threading.Event()
# 假活 ping BUSY / ping 慢：期权停重量 RPC，下一秒再 ping。
_optRpcBusy = threading.Event()
etfTicks = {}
etfDaily = {}
optionTicks = {}
optionDaily = {}
optionMeta = {}
_optionChainCodes = []
_optionMetaTriedAt = {}
_adjustedOptionCodes = set()
_snapJsonLock = threading.Lock()
_snapJsonCache = (0.0, b"")
_optionTickDlTried = set()
_optionTickDlAsOf = None
_optionTickRefreshMono = {}
_szoLiveDlMono = {}
_szoLiveRoundMono = 0.0
_szoLiveRoundPhase = ""
_shoMinuteOiLastRoundMono = 0.0
_shoMinuteOiLastSuccessMono = 0.0
_shoMinuteOiCircuitUntilMono = 0.0
_shoMinuteOiSubmitMono = {}
_shoMinuteOiEligibleCodes = set()
_shoMinuteOiUnavailableUntilMono = {}
_shoMinuteOiSuccessMono = {}
_shoMinuteOiCollapseRounds = 0
_shoMinuteOiSeedAsof = None
_shoMinuteOiLeaseShadowLastLogMono = 0.0
_shoMinuteOiLeasePlanShadowLastLogMono = 0.0
_shoMinuteOiHealth = {
    "enabled": bool(OPT_SHO_MIN_OI_ENABLE),
    "submitted": 0,
    "healthy": False,
    "elapsedSec": 0.0,
    "files": 0,
    "observed": 0,
    "applied": 0,
    "reason": "startup",
}
_optionListedToday = set()
_optionListedTodayAsOf = None
_optionDailyDlTried = set()
_optionDailyDlAsOf = None
_lastOptDailyMono = 0.0
_lastEtfDailyMono = 0.0
_lastSzoDailyDlMono = 0.0
_lastListedDailyDlMono = 0.0
_listedDailyDlCursor = 0
_szoSubOwned = set()
_szoSubConfirmAsof = None
_szoSubLastSyncMono = 0.0
_szoSubUnavailable = False
_szoSubRetryMono = 0.0
_bridgeRevisionOk = False
_bridgeRevision = ""
_optSubRevisionRefuseLogged = False
_optHealthDeadPushMono = 0.0
_lastDailyGateMode = None
optionStats = {
    "rounds": 0,
    "okTicks": 0,
    "failChunks": 0,
    "uniqueCodes": set(),
    "lastRoundSec": 0.0,
    "lastChunkMs": 0.0,
    "lastWall": None,
    "lastPushMono": 0.0,
    "sampleFiveBand": 0,
    "sampleTicks": 0,
}
stats = {
    "batches": 0,
    "tickUpdates": 0,
    "uniqueCodes": set(),
    "lastPushMono": 0.0,
    "lastPushWall": None,
    "subscribedMono": 0.0,
    "etfProbeN": 0,
    "maxGapMs": 0.0,
    "resubOk": 0,
    "resubFail": 0,
    "sampleFiveBand": 0,
    "sampleTicks": 0,
}


class WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Windows 轮转文件被占用时重试并降级，避免 PermissionError。"""

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        currentTime = int(time.time())
        t = self.rolloverAt - self.interval
        timeTuple = time.gmtime(t) if self.utc else time.localtime(t)
        dfn = self.rotation_filename(
            self.baseFilename + "." + time.strftime(self.suffix, timeTuple)
        )
        if os.path.exists(dfn):
            try:
                os.remove(dfn)
            except OSError:
                pass
        renamed = False
        for attempt in range(3):
            try:
                if os.path.exists(self.baseFilename):
                    os.rename(self.baseFilename, dfn)
                renamed = True
                break
            except OSError as e:
                if e.errno == errno.EACCES or (getattr(e, "winerror", None) == 32):
                    if attempt < 2:
                        time.sleep(0.2 * (attempt + 1))
                    continue
                # 其它错误也不抛到业务线程，避免 --- Logging error ---
                break
        if not renamed:
            # 跳过本轮轮转，继续写原文件
            if not self.delay:
                self.stream = self._open()
            nextRolloverAt = self.computeRollover(currentTime)
            while nextRolloverAt <= currentTime:
                nextRolloverAt += self.interval
            self.rolloverAt = nextRolloverAt
            return
        if self.backupCount > 0:
            for s in self.getFilesToDelete():
                try:
                    os.remove(s)
                except OSError:
                    pass
        if not self.delay:
            self.stream = self._open()
        nextRolloverAt = self.computeRollover(currentTime)
        while nextRolloverAt <= currentTime:
            nextRolloverAt += self.interval
        self.rolloverAt = nextRolloverAt


def isWatchdogWindow(now: datetime) -> bool:
    """假活检测窗口：[09:15, 11:30)、[13:00, 15:00)，本地时区。"""
    return watchdogWindowStart(now) is not None


def watchdogWindowStart(now: datetime):
    """本段窗口起点；窗口外返回 None。"""
    t = now.time()
    if dtTime(9, 15) <= t < dtTime(11, 30):
        return now.replace(hour=9, minute=15, second=0, microsecond=0)
    if dtTime(13, 0) <= t < dtTime(15, 0):
        return now.replace(hour=13, minute=0, second=0, microsecond=0)
    return None


def effectiveSilenceSec(
    now: datetime,
    nowMono: float,
    lastPushMono: float,
    subscribedMono: float = 0.0,
    graceSec: float = SUBSCRIBE_GRACE_SEC,
) -> float:
    """静默秒数从 max(lastPush, 本段窗口起点) 起算，避免午休累积触发 13:00 误拆。
    从未收到 ZMQ 增量时：宽限内当 0，其后用窗口已过秒数，才能在 11:09 空订后立刻进入假活。"""
    start = watchdogWindowStart(now)
    if lastPushMono <= 0:
        if start is None:
            return 0.0
        aged = max(0.0, float(nowMono) - float(subscribedMono or 0.0))
        if float(subscribedMono or 0.0) > 0.0 and aged < float(graceSec):
            return 0.0
        return max(0.0, (now - start).total_seconds())
    raw = max(0.0, nowMono - lastPushMono)
    if start is None:
        return raw
    sinceWindow = max(0.0, (now - start).total_seconds())
    return min(raw, sinceWindow)


def subscribeLooksNoop(elapsedSec: float, fastSec: float = SUBSCRIBE_FAST_SEC) -> bool:
    """快速 ACK 只表示疑似复用 combo，不能据此断言没有后续 callback。"""
    try:
        return float(elapsedSec) < float(fastSec)
    except (TypeError, ValueError):
        return False


def shouldEmitNamedLog(store, key, nowMono, cooldownSec) -> bool:
    """同 key 在 cooldown 内只放行一次；store 就地更新。"""
    if not isinstance(store, dict):
        return True
    try:
        last = float(store.get(key) or 0.0)
        now = float(nowMono)
        cool = float(cooldownSec)
    except (TypeError, ValueError):
        return True
    if (now - last) < cool:
        return False
    store[key] = now
    return True


def emitCooldownLog(logger, key, level, msg, *args, cooldownSec=None):
    """WARNING/ERROR 限频；冷却期内落到 DEBUG。"""
    cool = OPT_WARN_COOLDOWN_SEC if cooldownSec is None else float(cooldownSec)
    if shouldEmitNamedLog(_optLogLastMono, key, time.monotonic(), cool):
        logger.log(int(level), msg, *args)
    else:
        logger.debug(msg, *args)


def shouldDeferOptionHeavyRpc(
    uniqueN: int,
    inWindow: bool,
    silenceSec: float = 0.0,
    quoteHold: bool = False,
    pingBusy: bool = False,
) -> bool:
    """停 get_option_ticks/1m/1d，把策略线程还给 ContextInfo 回调。

    unique=0 只覆盖冷启动。盘中 unique 只增不减，假活时必须看静默/BUSY/重订占锁。
    """
    if quoteHold:
        return True
    if not inWindow:
        return False
    if pingBusy:
        return True
    if int(uniqueN or 0) <= 0:
        return True
    return float(silenceSec or 0.0) >= float(OPT_YIELD_SILENCE_SEC)


def shouldRunOptionQtyPatch(
    inWindow: bool,
    hasCodes: bool,
    *,
    quoteHold: bool = False,
    yieldQuote: bool = False,
    quoteRecovery: bool = False,
    optionCallbackEmpty: bool = False,
    optionCallbackFallback: bool = False,
) -> bool:
    """仅在全推健康且不处于主动恢复时运行重型 QMT OI 补丁。

    ``pollOptionQtyPatchRound`` 走 ``get_market_data_ex``，在当前大 QMT 策略中
    与 whole-quote callback 串行。期权 callback 刚归零或正在重建时必须让恢复优先；
    但确认失败并进入稳定 TDX fallback 后，若 whole-quote 仍健康，则恢复有界的
    6/8/10 张串行 OI 轮询，避免两条 TDX 链永久失去持仓量覆盖。
    """
    if (
        not inWindow
        or not hasCodes
        or quoteHold
        or quoteRecovery
        or (optionCallbackEmpty and not optionCallbackFallback)
    ):
        return False
    if yieldQuote and not OPT_QTY_WHEN_QUOTE_DEAD:
        return False
    return True


def optionChunkOiGateDecision(
    snapshot,
    *,
    nowMono: float,
    lastActionMono: float = 0.0,
    inWindow: bool = True,
    enabled: bool | None = None,
) -> dict:
    """决定非关键 ``OPT_CHUNK`` 是否应暂时让出 QMT RPC 给 OI。

    这是旁路纯函数：不发 RPC、不改变 OI 计划。只有最近一次 OI 诊断仍新鲜，
    且需求等待/P0 OI 年龄/轮次间隔出现压力时才延后；压力持续达到探测间隔后
    放行一次探测，避免行情补齐永久饥饿。``nextActionMono`` 由调用方保存，
    便于单开关回退和单元测试。
    """
    gateEnabled = (
        OPT_CHUNK_OI_GATE_ENABLE if enabled is None else bool(enabled)
    )
    base = {
        "defer": False,
        "probe": False,
        "reason": "pressure_clear",
        "nextActionMono": float(lastActionMono or 0.0),
        "snapshotAgeSec": None,
        "demandN": 0,
        "waitP95Sec": 0.0,
        "p0OiMaxSec": 0.0,
        "gapSec": 0.0,
    }
    if not gateEnabled:
        base["reason"] = "disabled"
        return base
    if not inWindow:
        base["reason"] = "out_of_window"
        return base
    snap = snapshot if isinstance(snapshot, dict) else {}
    try:
        now = float(nowMono)
    except (TypeError, ValueError):
        base["reason"] = "invalid_clock"
        return base
    try:
        observed = float(snap.get("observedMono") or 0.0)
    except (TypeError, ValueError):
        observed = 0.0
    age = max(0.0, now - observed) if observed > 0.0 else float("inf")
    base["snapshotAgeSec"] = round(age, 1) if math.isfinite(age) else None
    if observed <= 0.0 or age > OPT_CHUNK_OI_GATE_STATE_MAX_AGE_SEC:
        base["reason"] = "pressure_snapshot_stale"
        return base

    def nonNegative(name: str) -> float:
        try:
            value = float(snap.get(name) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value >= 0.0 else 0.0

    try:
        demandN = max(0, int(snap.get("demandN") or 0))
    except (TypeError, ValueError):
        demandN = 0
    waitP95 = nonNegative("demandWaitP95Sec")
    p0OiMax = nonNegative("p0OiAgeMaxSec")
    gapSec = nonNegative("gapSec")
    base.update(
        {
            "demandN": demandN,
            "waitP95Sec": waitP95,
            "p0OiMaxSec": p0OiMax,
            "gapSec": gapSec,
        }
    )
    reasons = []
    if demandN >= OPT_CHUNK_OI_GATE_MIN_DEMAND_N:
        if waitP95 >= OPT_CHUNK_OI_GATE_WAIT_P95_SEC:
            reasons.append("demand_wait_high")
        if gapSec >= OPT_CHUNK_OI_GATE_GAP_SEC:
            reasons.append("oi_gap_high")
    if demandN > 0 and p0OiMax >= OPT_CHUNK_OI_GATE_P0_MAX_SEC:
        reasons.append("p0_oi_age_high")
    if not reasons:
        return base

    previous = 0.0
    try:
        previous = float(lastActionMono or 0.0)
    except (TypeError, ValueError):
        previous = 0.0
    reason = "+".join(reasons)
    if previous <= 0.0:
        base.update(
            {
                "defer": True,
                "reason": reason,
                "nextActionMono": now,
            }
        )
        return base
    if now - previous >= OPT_CHUNK_OI_GATE_PROBE_INTERVAL_SEC:
        base.update(
            {
                "probe": True,
                "reason": "pressure_probe_due:" + reason,
                "nextActionMono": now,
            }
        )
        return base
    base.update({"defer": True, "reason": reason, "nextActionMono": previous})
    return base


def updateOptionQtyPressureSnapshot(diag, *, gapSec: float, nowMono: float) -> None:
    """保存最近 OI 诊断供 OPT_CHUNK 门控只读使用。"""
    if not isinstance(diag, dict):
        return
    with _qtyPressureLock:
        _qtyP0PressureSnapshot.update(
            {
                "observedMono": float(nowMono),
                "demandN": int(diag.get("demand") or 0),
                "demandWaitP95Sec": float(diag.get("demand_wait_p95_sec") or 0.0),
                "p0OiAgeMaxSec": float(diag.get("p0_oi_age_max_sec") or 0.0),
                "gapSec": max(0.0, float(gapSec or 0.0)),
            }
        )


def optionQtyPressureSnapshot() -> dict:
    """返回最近 OI 压力快照副本，避免门控读取时与更新互相干扰。"""
    with _qtyPressureLock:
        return dict(_qtyP0PressureSnapshot)


def shoMinuteOiRuntimeEnabled(
    configEnabled: bool | None = None,
    disableFileExists: bool | None = None,
) -> bool:
    """510050 分钟 OI 运行时开关；flag 文件可在不重启时立即回退。"""
    configured = OPT_SHO_MIN_OI_ENABLE if configEnabled is None else bool(configEnabled)
    if disableFileExists is None:
        disableFileExists = os.path.exists(OPT_SHO_MIN_OI_DISABLE_FILE)
    return configured and not bool(disableFileExists)


def shouldRunShoMinuteOiDownload(
    *,
    now: datetime,
    enabled: bool,
    uniqueN: int,
    silenceSec: float,
    quoteHold: bool,
    rpcBusy: bool,
    quoteRecovery: bool,
    circuitUntilMono: float,
    lastStartMono: float,
    nowMono: float,
    intervalSec: float = OPT_SHO_MIN_OI_INTERVAL_SEC,
) -> bool:
    """分钟 OI 下载硬门闩：只在连续竞价、全推健康且无恢复/熔断时运行。"""
    if (
        not enabled
        or not isOptionContinuousAuction(now)
        or quoteHold
        or rpcBusy
        or quoteRecovery
        or float(nowMono) < float(circuitUntilMono or 0.0)
    ):
        return False
    if not isWholeQuoteHealthyForOptDl(uniqueN, silenceSec):
        return False
    last = float(lastStartMono or 0.0)
    return last <= 0.0 or (float(nowMono) - last) >= float(intervalSec)


def shoMinuteOiRoundHealthy(
    *,
    submittedN: int,
    plannedN: int,
    elapsedSec: float,
    maxSubmitSec: float,
    fileN: int,
    observedN: int,
    observationAgeSec: float = 0.0,
    maxObservationAgeSec: float = OPT_SHO_MIN_OI_MAX_OBS_AGE_SEC,
) -> bool:
    """这里只判断共享 QMT 通道是否健康；单张缺 OI 交给逐合约冷却。"""
    planned = max(0, int(plannedN))
    return bool(
        planned > 0
        and int(submittedN) == planned
        and float(elapsedSec) <= float(maxSubmitSec)
    )


def shoMinuteOiCircuitDecision(
    *,
    submittedN: int,
    plannedN: int,
    elapsedSec: float,
    maxSubmitSec: float,
    eligiblePlannedN: int,
    eligibleObservedN: int,
    previousCollapseRounds: int,
) -> tuple[bool, int, str]:
    """区分通道硬故障与单张自然无分钟 OI；已知可用集连续塌陷才熔断。"""
    if int(plannedN) <= 0 or int(submittedN) != int(plannedN):
        return True, 0, "transport_incomplete"
    if float(elapsedSec) > float(maxSubmitSec):
        return True, 0, "transport_slow"
    known = max(0, int(eligiblePlannedN))
    observed = max(0, int(eligibleObservedN))
    collapse = known >= 4 and observed * 2 < known
    if collapse:
        rounds = max(0, int(previousCollapseRounds)) + 1
        if rounds >= 2:
            return True, rounds, "eligible_collapse"
        return False, rounds, "eligible_collapse_pending"
    return False, 0, "partial_allowed"


def shoMinuteOiOffloadCodes(nowMono: float | None = None) -> set:
    """只返回最近成功的具体合约；永不因一批成功而卸载整条 510050 长尾。"""
    nowValue = time.monotonic() if nowMono is None else float(nowMono)
    if (
        not shoMinuteOiRuntimeEnabled()
        or nowValue < float(_shoMinuteOiCircuitUntilMono or 0.0)
    ):
        return set()
    return {
        code
        for code, successMono in _shoMinuteOiSuccessMono.items()
        if successMono > 0
        and (nowValue - float(successMono)) <= OPT_SHO_MIN_OI_ACTIVE_TTL_SEC
    }


def shoMinuteOiOffloadActive(nowMono: float | None = None) -> bool:
    """兼容旧健康字段：至少一张合约正由分钟路径接管时才为真。"""
    return bool(shoMinuteOiOffloadCodes(nowMono))


def shoMinuteOiHealthSnapshot(nowMono: float | None = None) -> dict:
    """导出可观测状态；运行时 flag、熔断剩余时间与最近成功年龄动态计算。"""
    nowValue = time.monotonic() if nowMono is None else float(nowMono)
    out = dict(_shoMinuteOiHealth)
    out["runtimeEnabled"] = shoMinuteOiRuntimeEnabled()
    out["offloadActive"] = shoMinuteOiOffloadActive(nowValue)
    offloadCodes = shoMinuteOiOffloadCodes(nowValue)
    unavailableN = sum(
        1
        for until in _shoMinuteOiUnavailableUntilMono.values()
        if float(until or 0.0) > nowValue
    )
    out["eligibleN"] = len(_shoMinuteOiEligibleCodes)
    out["offloadN"] = len(offloadCodes)
    out["offloadSample"] = sorted(offloadCodes)[:5]
    out["unavailableN"] = unavailableN
    out["fallbackN"] = max(0, int(out.get("universeN") or 0) - len(offloadCodes))
    out["circuitRemainingSec"] = round(
        max(0.0, float(_shoMinuteOiCircuitUntilMono or 0.0) - nowValue), 1
    )
    out["lastSuccessAgeSec"] = (
        round(max(0.0, nowValue - float(_shoMinuteOiLastSuccessMono)), 1)
        if _shoMinuteOiLastSuccessMono > 0
        else None
    )
    return out


def markOptRpcBusy(busy: bool) -> None:
    if busy:
        _optRpcBusy.set()
    else:
        _optRpcBusy.clear()


def isOptRpcBusy() -> bool:
    return _optRpcBusy.is_set()


def wholeQuoteSilenceSec(now=None, nowMono=None) -> float:
    now = now or datetime.now()
    nowMono = time.monotonic() if nowMono is None else nowMono
    with _cacheLock:
        lastPush = stats["lastPushMono"]
        subscribedMono = float(stats.get("subscribedMono") or 0.0)
    return effectiveSilenceSec(now, nowMono, lastPush, subscribedMono)


def shouldProbeEtfTicks(
    uniqueN: int,
    nowMono: float,
    lastProbeMono: float,
    intervalSec: float,
    silenceSec: float = 0.0,
) -> bool:
    """unique 只增不减：全推假死后集合仍非空，须用静默判断是否探针。"""
    stale = float(silenceSec or 0.0) >= float(OPT_YIELD_SILENCE_SEC)
    if int(uniqueN or 0) > 0 and not stale:
        return False
    if float(lastProbeMono or 0.0) <= 0.0:
        return True
    return (float(nowMono) - float(lastProbeMono)) >= float(intervalSec)


def etfProbeIntervalSec(isWindow: bool) -> float:
    return ETF_PROBE_LIVE_SEC if isWindow else ETF_PROBE_IDLE_SEC


def probeEtfCodesSafe(codes) -> list:
    """只允许带交易所后缀的代码列表，禁止 SH/SZ/BJ 市场码。"""
    out = []
    banned = {"SH", "SZ", "BJ", "HK"}
    for raw in codes or ():
        code = str(raw or "").strip().upper()
        if not code or code in banned or "." not in code:
            continue
        out.append(str(raw).strip())
    return out


def wholeQuoteUniqueN() -> int:
    with _cacheLock:
        return len(stats["uniqueCodes"])


def applyEtfProbeTicks(payload) -> int:
    """把五只 ETF 探针写入 etfTicks，不计入全推 unique / lastPush。"""
    if not isinstance(payload, dict):
        return 0
    n = 0
    with _cacheLock:
        for code in ETF_TICK_CODE_SET:
            tick = payload.get(code)
            if not isinstance(tick, dict) or not tick:
                continue
            etfTicks[code] = tick
            n += 1
        stats["etfProbeN"] = n
    if n:
        invalidateQuoteSnapshotJson()
    return n


def probeEtfWholeQuoteTicks(xtdata, codes=OPT_UNDERLYING_QMT) -> dict:
    """交易中心探针：仅 5 只标的代码列表，禁止市场码全扫。"""
    safe = probeEtfCodesSafe(codes)
    if not safe:
        return {}
    getter = getattr(xtdata, "get_full_tick", None)
    if getter is None:
        return {}
    raw = getter(safe, timeout_seconds=3.0) or {}
    if not isinstance(raw, dict):
        return {}
    return {c: raw[c] for c in safe if isinstance(raw.get(c), dict)}


def decideResubConfirm(
    nowMono: float,
    confirmSince: float,
    lastPushMono: float,
    confirmSec: float = RESUB_CONFIRM_SEC,
) -> str:
    """idle / wait / ok / fail。再订后必须见到新增量才算成功。"""
    if confirmSince <= 0:
        return "idle"
    if lastPushMono > confirmSince:
        return "ok"
    if nowMono - confirmSince >= confirmSec:
        return "fail"
    return "wait"


def defaultInstanceLockPath() -> str:
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(tmp, INSTANCE_LOCK_NAME)


def envFlagOn(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "on", "y"):
        return True
    if val in ("0", "false", "no", "off", "n"):
        return False
    return default


_QUOTE_KILL_FORBIDDEN = ("xtitclient", "rt_monitor_app.py", "quote-live")


def quoteScriptBasename() -> str:
    return os.path.basename(os.path.abspath(__file__))


def isQuoteLiveCmdline(cmdline: str) -> bool:
    """另一套全推客户端，会把 7214 combo 引用计数顶上去。"""
    low = str(cmdline or "").lower()
    return "quote-live" in low or "quote_live" in low


def isDaqmtQuoteCmdline(cmdline: str) -> bool:
    """只认本脚本。桥接 XtItClient / Flask / quote-live 一律不是。"""
    raw = str(cmdline or "")
    low = raw.lower()
    if not raw.strip():
        return False
    if isQuoteLiveCmdline(raw):
        return False
    if any(mark in low for mark in _QUOTE_KILL_FORBIDDEN):
        return False
    base = quoteScriptBasename()
    if base and base in raw:
        return True
    return "qmt实时行情.py" in low


def shouldKillQuoteListener(cmdline: str, port: int) -> bool:
    if isDaqmtQuoteCmdline(cmdline):
        return True
    low = str(cmdline or "").lower()
    if not low or any(mark in low for mark in _QUOTE_KILL_FORBIDDEN):
        return False
    return int(port) == int(QUOTE_HTTP_PORT) and "python" in low


def cmdlineForPid(pid: int) -> str:
    try:
        import psutil

        return " ".join(psutil.Process(int(pid)).cmdline() or [])
    except Exception:
        pass
    if os.name != "nt":
        try:
            with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
                return fh.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        except OSError:
            return ""
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                '(Get-CimInstance Win32_Process -Filter "ProcessId=%d").CommandLine' % int(pid),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def listenPidsOnPort(port: int) -> list:
    pids = []
    seen = set()
    try:
        kw = {
            "capture_output": True,
            "text": True,
            # 必须显式给编码：中文 Windows 下 netstat/powershell 输出为 GBK，
            # 默认按 UTF-8 解会让 subprocess._readerthread 抛 UnicodeDecodeError，
            # 线程静默死亡 → 单实例/自愈探测失效。
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 20,
        }
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(["netstat", "-ano"], **kw)
    except Exception:
        return []
    needle = ":%s" % int(port)
    for line in (r.stdout or "").splitlines():
        if "LISTEN" not in line.upper() or needle not in line:
            continue
        parts = line.split()
        if not parts or not parts[-1].isdigit():
            continue
        pid = int(parts[-1])
        if pid in seen:
            continue
        seen.add(pid)
        pids.append(pid)
    return pids


def readLockFilePid(lockPath: str):
    try:
        with open(lockPath, "rb") as fh:
            text = fh.read(32).decode("ascii", errors="ignore").strip()
    except OSError:
        return None
    token = (text.split() or [""])[0]
    if token.isdigit():
        return int(token)
    return None


def scanQuoteScriptPids() -> list:
    out = []
    try:
        import psutil
    except ImportError:
        return out
    skip = protectedPids()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid in skip or not isPythonProcess(pid):
                continue
            cl = " ".join(proc.info.get("cmdline") or [])
            if isDaqmtQuoteCmdline(cl):
                out.append(pid)
        except Exception:
            continue
    return out


def scanQuoteLivePids() -> list:
    """只报警，不杀。quote-live 与本脚本抢同一 combo。"""
    out = []
    try:
        import psutil
    except ImportError:
        return out
    skip = protectedPids()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid in skip:
                continue
            cl = " ".join(proc.info.get("cmdline") or [])
            if isQuoteLiveCmdline(cl):
                out.append(pid)
        except Exception:
            continue
    return out


def enforceSoleQuoteInstance(logger=None, lockPath=None, port=None) -> list:
    """运行中再扫一遍本脚本 python，漏网的杀掉。"""
    lockPath = lockPath or defaultInstanceLockPath()
    port = int(port if port is not None else QUOTE_HTTP_PORT)
    leftover = [
        pid for pid in collectPreviousQuotePids(lockPath, port)
        if pid not in protectedPids()
    ]
    if not leftover:
        return []
    return stealPreviousQuoteInstance(logger, lockPath, port)


def protectedPids() -> set:
    out = {int(os.getpid())}
    try:
        out.add(int(os.getppid()))
    except OSError:
        pass
    return out


def isPythonProcess(pid: int) -> bool:
    """Cursor/bash 包装进程命令行也带脚本路径，只能杀 python.exe。"""
    try:
        import psutil

        name = (psutil.Process(int(pid)).name() or "").lower()
        return name.startswith("python")
    except Exception:
        pass
    cmd = cmdlineForPid(pid).lower()
    if not cmd or "bash" in cmd or "cmd.exe" in cmd:
        return False
    return cmd.startswith("python") or "\\python.exe" in cmd or "/python.exe" in cmd


def killPidHard(pid: int) -> bool:
    if int(pid) <= 0 or int(pid) in protectedPids():
        return False
    try:
        if os.name == "nt":
            kw = {"capture_output": True, "timeout": 20}
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(["taskkill", "/F", "/PID", str(int(pid))], **kw)
        else:
            os.kill(int(pid), signal.SIGTERM)
    except Exception:
        return False
    return True


def collectPreviousQuotePids(lockPath: str, port: int) -> list:
    skip = protectedPids()
    found = set()
    for pid in listenPidsOnPort(port):
        if pid in skip or not isPythonProcess(pid):
            continue
        if shouldKillQuoteListener(cmdlineForPid(pid), port):
            found.add(pid)
    lockPid = readLockFilePid(lockPath)
    if lockPid and lockPid not in skip and isPythonProcess(lockPid):
        cmd = cmdlineForPid(lockPid)
        if isDaqmtQuoteCmdline(cmd) or shouldKillQuoteListener(cmd, port):
            found.add(lockPid)
    found.update(scanQuoteScriptPids())
    return sorted(found)


def stealPreviousQuoteInstance(logger=None, lockPath=None, port=None) -> list:
    """默认先停旧实例再启动。DAQMT_QUOTE_STEAL_LOCK=0 可关。不杀桥接/Flask。"""
    if not envFlagOn("DAQMT_QUOTE_STEAL_LOCK", True):
        return []
    lockPath = lockPath or defaultInstanceLockPath()
    port = int(port if port is not None else QUOTE_HTTP_PORT)
    killed = []
    for pid in collectPreviousQuotePids(lockPath, port):
        if logger is not None:
            logger.info("终止旧大QMT实时行情 PID=%s", pid)
        if killPidHard(pid):
            killed.append(pid)
    if killed:
        time.sleep(STEAL_SETTLE_SEC)
    return killed


def acquireInstanceLockOrSteal(lockPath: str, logger=None):
    lockFile = tryAcquireInstanceLock(lockPath)
    if lockFile is not None:
        return lockFile
    stealPreviousQuoteInstance(logger, lockPath)
    lockFile = tryAcquireInstanceLock(lockPath)
    if lockFile is not None:
        return lockFile
    time.sleep(0.8)
    leftover = [
        pid for pid in collectPreviousQuotePids(lockPath, QUOTE_HTTP_PORT)
        if pid not in protectedPids()
    ]
    if leftover and logger is not None:
        logger.error("实例锁仍被占 leftover_pid=%s", leftover)
    return tryAcquireInstanceLock(lockPath)


def tryAcquireInstanceLock(lockPath: str):
    """进程级互斥，避免双开导致 combo 引用计数>1、退订拆不掉句柄。"""
    lockFile = open(lockPath, "a+b")
    try:
        lockFile.seek(0, os.SEEK_END)
        if lockFile.tell() == 0:
            lockFile.write(b"\0")
            lockFile.flush()
        lockFile.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lockFile.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lockFile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lockFile.close()
        return None
    try:
        lockFile.seek(0)
        lockFile.write(("%d\n" % os.getpid()).encode("ascii"))
        lockFile.flush()
    except OSError:
        pass
    return lockFile


def releaseInstanceLock(lockFile) -> None:
    if lockFile is None:
        return
    try:
        lockFile.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lockFile.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lockFile.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        lockFile.close()
    except OSError:
        pass


def quoteUniqueCriticallyLow(
    uniqueN: int,
    threshold: int = QUOTE_CRITICAL_UNIQUE_N,
) -> bool:
    """全推增量是否「从未真正进缓存」：unique 只有个位数即视为 combo 未订上。

    判据与 ping 快慢解耦——桥满载时 ping 3~6s 甚至 busy，纯 ping 判据会永久
    放弃自救（2026-09-11 现场：unique=1 持续十余分钟、resub_ok=0）。
    """
    try:
        n = int(uniqueN)
    except (TypeError, ValueError):
        return False
    return 0 <= n < int(threshold)


def shouldResubOnBusySilence(
    busyStreak: int,
    silenceSec: float,
    busyMax: int = QUOTE_BUSY_RESUB_STREAK,
    silenceThreshold: float = SILENCE_SECONDS,
) -> bool:
    """ping 连续 busy 且全推持续静默 → 允许强制重订。

    背景（2026-09-11）：盘中 `_rpcLock` 被期权 RPC 长期占用，`tryRpcPingMs`
    永远返回 busy，旧逻辑只打日志继续，导致全推死了十馀分钟不自愈。
    busy 说明桥还在（不是断线），因此静默只能是订阅侧失效，重订是正确动作。
    """
    try:
        n = int(busyStreak)
    except (TypeError, ValueError):
        return False
    return n >= int(busyMax) and float(silenceSec) >= float(silenceThreshold)


def shouldTreatAsFakeAlive(
    pingMs,
    silenceSec: float,
    pingMaxMs: float = PING_FAKE_ALIVE_MAX_MS,
    silenceThreshold: float = SILENCE_SECONDS,
    uniqueN: int = -1,
) -> bool:
    """真假活：ping 快（桥还在）且全推静默才敢重订。

    另加一条**独立判据**（2026-09-11）：`uniqueN` 长时间只有个位数说明增量
    从未真正进过缓存（combo 没订上），与 ping 快慢无关。
    桥满载时 ping 稳定 3~6s，旧逻辑会一直"让路"→ 全推死了也不自救。
    """
    try:
        ms = float(pingMs)
    except (TypeError, ValueError):
        return False
    if ms < 0.0:
        return False
    if float(silenceSec) < float(silenceThreshold):
        return False
    if ms <= float(pingMaxMs):
        return True
    return 0 <= int(uniqueN) < int(QUOTE_CRITICAL_UNIQUE_N)


def decideWatchdogAction(
    isWindow: bool,
    silenceSec: float,
    consecutiveFail: int,
    silenceThreshold: float = SILENCE_SECONDS,
    failMax: int = RESUB_FAIL_MAX,
) -> str:
    """idle / check_ping / give_up。check_ping 后需 ping 成功且退订成功才再订。"""
    if consecutiveFail >= failMax:
        return "give_up"
    if not isWindow:
        return "idle"
    if silenceSec < silenceThreshold:
        return "idle"
    return "check_ping"


def canSubscribeAfterUnsubscribe(pingOk: bool, unsubOk: bool) -> bool:
    return bool(pingOk) and bool(unsubOk)


def decideGiveUpNext(
    nowMono: float,
    giveUpSince: float,
    retrySec: float = GIVE_UP_RETRY_SEC,
) -> str:
    """放弃态：hold 继续等待；retry 冷却到期，清失败计数后再检测。无需人工重启。"""
    if giveUpSince <= 0:
        return "hold"
    if nowMono - giveUpSince >= retrySec:
        return "retry"
    return "hold"


def tickHasFiveBand(tick) -> bool:
    """至少一侧买卖价 >0。全 0 列表不算有盘口。"""
    if not isinstance(tick, dict):
        return False
    for key in ("askPrice", "bidPrice", "askPrice1", "bidPrice1", "ask1", "bid1"):
        val = tick.get(key)
        if isinstance(val, (list, tuple)):
            for item in val:
                n = _asFloat(item)
                if n is not None and n > 0:
                    return True
            continue
        n = _asFloat(val)
        if n is not None and n > 0:
            return True
    return False


def tsCodeToQmtOpt(tsCode: str) -> str:
    """Tushare 8 位期权 ts_code → QMT .SHO/.SZO；6 位股票代码拒绝。"""
    text = str(tsCode or "").strip().upper()
    if text.endswith(".SHO") or text.endswith(".SZO"):
        body = text.rsplit(".", 1)[0]
        if len(body) == 8 and body.isdigit():
            return text
        raise ValueError("unsupported option ts_code: %s" % tsCode)
    if text.endswith(".SH") or text.endswith(".SZ"):
        body, mkt = text.rsplit(".", 1)
        if len(body) != 8 or not body.isdigit():
            raise ValueError("unsupported option ts_code: %s" % tsCode)
        return body + (".SHO" if mkt == "SH" else ".SZO")
    raise ValueError("unsupported option ts_code: %s" % tsCode)


def qmtOptToTsCode(qmtCode: str) -> str:
    """QMT .SHO/.SZO → Tushare ts_code .SH/.SZ；其余原样返回。"""
    text = str(qmtCode or "").strip().upper()
    if text.endswith(".SHO"):
        return text[:-4] + ".SH"
    if text.endswith(".SZO"):
        return text[:-4] + ".SZ"
    return text


def _asFloat(value):
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x != x:
        return None
    return x


def optionTickStampFieldSources(tick, source: str) -> None:
    """仅给本源实际提供的字段标记来源，不能把整条记录的来源外溢到空字段。"""
    if not isinstance(tick, dict):
        return
    src = str(source or "").strip().lower()
    if src not in {"qmt", "tdx"}:
        return

    last = _asFloat(tick.get("lastPrice"))
    if last is None:
        last = _asFloat(tick.get("last"))
    vol = _asFloat(tick.get("vol"))
    if vol is None:
        vol = _asFloat(tick.get("volume"))
    oi = _asFloat(tick.get("oi"))
    if oi is None:
        oi = _asFloat(tick.get("openInt"))
    if oi is None:
        oi = _asFloat(tick.get("openInterest"))
    provided = {
        "lastSource": last is not None and last > 0,
        "qtySource": vol is not None and vol >= 0,
        "oiSource": oi is not None and oi >= 0,
        "bookSource": tickHasFiveBand(tick),
    }
    for key, hasValue in provided.items():
        if hasValue:
            tick[key] = src
        else:
            tick.pop(key, None)


# 与全推 ZMQ slim 对齐：决策字段全留，五档截前 5。不把 7230 只塞进本 HTTP。
SNAPSHOT_TICK_KEYS = (
    "lastPrice",
    "open",
    "high",
    "low",
    "lastClose",
    "volume",
    "amount",
    "openInt",
    "openInterest",
    "stockStatus",
    "bidPrice",
    "bidVol",
    "askPrice",
    "askVol",
    "bidPrice1",
    "askPrice1",
    "bidVol1",
    "askVol1",
    "time",
    "stime",
    # 字段级时钟（2026-09-14 第二次修复，LESSON-020）：
    # 第一次只是把它们透出（LESSON-019），结果 510050 灰行 40%→72% —— 因为当时
    # 这些时钟只被 QMT 补丁驱动（`OPT_QTY patch n=10` 每轮 10 张 / 全链 650 张
    # ≈ 每合约 260s 才轮一次），而 T 板 pick() 是「键在就只认它」。
    # 现在已在 `optionTickStampTdxFieldClocks` 里让 TDX 供的字段按**每轮 TDX 喂数**
    # 重新计时（观测时刻 = 合并墙钟），字段时钟才配得上它的语义，可以上板。
    "lastTime",
    "qtyTime",
    "oiTime",
    "bookTime",
    # `*Time` 是最后一次可信观测；`*ChangedTime` 仅在对应数值真的变化时前进。
    # 页面同时展示两者，避免「数值未变」被误判为「没有刷新」。
    "lastChangedTime",
    "qtyChangedTime",
    "oiChangedTime",
    "bookChangedTime",
    # 字段来源供轻量 delta 逐单元格说明；不是原始行情字段但必须随快照透出。
    "lastSource",
    "qtySource",
    "oiSource",
    "bookSource",
)
_SNAPSHOT_LIST_KEYS = frozenset(("bidPrice", "bidVol", "askPrice", "askVol"))
_SNAPSHOT_BOOK_LEVELS = 5


def decodeQuoteTickDate(t):
    """QMT time/stime → 日期。毫秒/秒时间戳；过小的数（如测试用 1）忽略。"""
    if t is None or t == "":
        return None
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date) and not isinstance(t, datetime):
        return t
    try:
        v = float(t)
    except (TypeError, ValueError):
        return None
    if v > 1e12:
        return datetime.fromtimestamp(v / 1000.0).date()
    if v > 1e11:
        s = str(int(v))
        if len(s) >= 8:
            try:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            except ValueError:
                return None
        return None
    if v > 1e9:
        return datetime.fromtimestamp(v).date()
    return None


def optionTickStampSec(t) -> float | None:
    """QMT tick time → unix 秒。无法解析则 None。"""
    if t is None or t == "":
        return None
    if isinstance(t, datetime):
        return t.timestamp()
    try:
        v = float(t)
    except (TypeError, ValueError):
        return None
    if v > 1e12:
        return v / 1000.0
    if v > 1e11:
        return None
    if v > 1e9:
        return v
    return None


def szoMinuteOiEffectiveTimeMs(rawTime, observedAtMs=None) -> float:
    """将 QMT 当前 1m 柱的结束时刻转换为可比较的 OI 事件时刻。

    QMT DAT 在分钟尚未结束时可能先写入下一分钟的结束标签，例如墙钟
    09:54:14 读取到 ``09:55:00``。这不是未来行情，应按该柱的起点
    ``09:54:00`` 参与年龄门控；但未来超过容忍范围或校正后仍在未来时，
    返回 0 让调用方 fail-closed。未提供观测墙钟时保持原始时间，兼容离线
    解析和历史测试。
    """
    rawSec = optionTickStampSec(rawTime)
    if rawSec is None:
        return 0.0
    rawMs = float(rawSec) * 1000.0
    try:
        observedMs = float(observedAtMs)
    except (TypeError, ValueError):
        observedMs = 0.0
    if observedMs <= 0.0 or rawMs <= observedMs:
        return rawMs
    futureSec = (rawMs - observedMs) / 1000.0
    alignLimit = float(OPT_SZO_MIN_FUTURE_BAR_ALIGN_SEC)
    if alignLimit <= 0.0:
        return rawMs
    if futureSec > alignLimit:
        return 0.0
    alignedMs = rawMs - float(OPT_SZO_MIN_BAR_SECONDS) * 1000.0
    return alignedMs if alignedMs <= observedMs else 0.0


def optionTickIsFresh(packed, asof, now=None, maxAgeSec: float = OPT_TICK_FRESH_SEC) -> bool:
    """当日有量 tick 且时钟未过期才算跟盘；灌库冻住的旧时刻不能盖 1d。"""
    if not isinstance(packed, dict) or asof is None:
        return False
    last = _asFloat(packed.get("last"))
    if last is None or last <= 0:
        return False
    vol = _asFloat(packed.get("vol"))
    if vol is None:
        vol = _asFloat(packed.get("volume"))
    if vol is None or vol <= 0:
        return False
    tickDay = decodeQuoteTickDate(packed.get("t"))
    if tickDay is not None and tickDay != asof:
        return False
    ts = optionTickStampSec(packed.get("t"))
    if ts is None:
        return False
    wall = now if isinstance(now, datetime) else datetime.now()
    age = wall.timestamp() - ts
    return 0.0 <= age <= float(maxAgeSec)


def compactQuoteTick(tick, *, fillLastFromMid: bool = True, asofDate=None) -> dict | None:
    """快照 slim 字段 + last/bid1/ask1/vol/oi/t。
    ETF 竞价 last=0 可用买卖中价；期权不要中价冒充成交。
    asofDate 非空且 tick 非当日：丢掉 last/vol/oi/盘口（废 tick 不能冒充今日成交）。"""
    if not isinstance(tick, dict):
        return None
    out = {}
    for key in SNAPSHOT_TICK_KEYS:
        if key not in tick:
            continue
        val = tick[key]
        if key in _SNAPSHOT_LIST_KEYS and isinstance(val, (list, tuple)):
            out[key] = list(val[:_SNAPSHOT_BOOK_LEVELS])
        else:
            out[key] = val
    last = _asFloat(out.get("lastPrice"))
    if last == 0:
        last = None
    bid1 = _asFloat(out.get("bidPrice1"))
    if bid1 is None:
        bid1 = _asFloat(out.get("bidPrice"))
    ask1 = _asFloat(out.get("askPrice1"))
    if ask1 is None:
        ask1 = _asFloat(out.get("askPrice"))
    if fillLastFromMid and last is None and bid1 is not None and ask1 is not None:
        last = (bid1 + ask1) / 2.0
        out["lastPrice"] = last
    oi = out.get("openInt")
    if oi is None:
        oi = out.get("openInterest")
    out["bid1"] = bid1
    out["ask1"] = ask1
    out["vol"] = _asFloat(out.get("volume"))
    out["oi"] = _asFloat(oi)
    out["t"] = out.get("time") or out.get("stime")
    # 保留通达信来源标记，供盘中区分 QMT/TDX（非 slim 行情字段）
    if tick.get("fromTdx"):
        out["fromTdx"] = True
    if tick.get("qtyFromQmt"):
        out["qtyFromQmt"] = True
    if asofDate is not None:
        tickDay = decodeQuoteTickDate(out.get("t"))
        if tickDay is not None and tickDay != asofDate:
            if last is not None:
                out["staleLast"] = last
            last = None
            out["vol"] = None
            out["oi"] = None
            out["bid1"] = None
            out["ask1"] = None
            out.pop("fromTdx", None)
            for bookKey in ("bidPrice", "askPrice", "bidVol", "askVol"):
                out.pop(bookKey, None)
    out["last"] = last
    return out


def parseYmdBarDate(value):
    """日K index/time → date。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    digits = "".join(ch for ch in str(value).strip() if ch.isdigit())
    if len(digits) >= 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            pass
    return decodeQuoteTickDate(value)


def optionDailyCloseConflictsBook(close, packed, ratioMin: float = 7.5, ratioMax: float = 12.5) -> bool:
    """新挂 1d 常见把权利金放大约 10 倍；相对 last/买卖档落在该倍数则弃用 close。"""
    closeN = _asFloat(close)
    if closeN is None or closeN <= 0:
        return False
    refs = []
    last = _asFloat((packed or {}).get("last"))
    if last is None:
        last = _asFloat((packed or {}).get("lastPrice"))
    if last is not None and last > 0:
        refs.append(last)
    bid = _asFloat((packed or {}).get("bid1"))
    if bid is None:
        bid = _asFloat((packed or {}).get("bidPrice"))
    ask = _asFloat((packed or {}).get("ask1"))
    if ask is None:
        ask = _asFloat((packed or {}).get("askPrice"))
    if bid is not None and bid > 0:
        refs.append(bid)
    if ask is not None and ask > 0:
        refs.append(ask)
    for ref in refs:
        ratio = closeN / ref
        if float(ratioMin) <= ratio <= float(ratioMax):
            return True
    return False


def optionDailyLastAllowed(
    close, packed, listedToday: bool = False, meta=None, spot=None,
) -> bool:
    """无盘口的新挂 1d 不许直接盖 last；能确认是 ×10 错价则允许用缩放值。"""
    if optionDailyCloseConflictsBook(close, packed):
        return False
    if listedToday and not tickHasFiveBand(packed):
        last = _asFloat((packed or {}).get("last"))
        if last is not None and last > 0:
            return True
        scaled = scaleTenXOptLast(close, meta, spot)
        raw = _asFloat(close)
        return (
            scaled is not None
            and raw is not None
            and abs(float(scaled) - float(raw)) > 1e-9
        )
    return True


def scaleTenXOptLast(last, meta=None, spot=None):
    """新挂 1m/1d 常见把权利金放大 10 倍；能缩成合理权利金则返回缩放值。"""
    lastN = _asFloat(last)
    if lastN is None or lastN <= 0:
        return None
    if not optionPremiumLooksImplausible(lastN, meta, spot):
        return lastN
    scaled = lastN / 10.0
    if scaled <= 0:
        return None
    if optionPremiumLooksImplausible(scaled, meta, spot):
        return None
    return scaled


def optionPremiumLooksImplausible(last, meta, spot) -> bool:
    """权利金不能高于标的/行权价；近平值却到 0.8 以上视为 1m/1d 10 倍错价。"""
    lastN = _asFloat(last)
    if lastN is None or lastN <= 0:
        return False
    meta = meta or {}
    k = _asFloat(meta.get("k"))
    cp = str(meta.get("cp") or "").upper()
    spotN = _asFloat(spot)
    if cp == "C" and spotN is not None and lastN > spotN + 0.05:
        return True
    if cp == "P" and k is not None and lastN > k + 0.05:
        return True
    if spotN is None or k is None or cp not in ("C", "P"):
        return False
    intrins = max(spotN - k, 0.0) if cp == "C" else max(k - spotN, 0.0)
    if intrins <= 0.08 and lastN >= 0.80:
        return True
    return lastN > intrins * 8.0 + 0.45


def etfSpotByUndlSix(etfTickMap=None, etfDailyMap=None) -> dict:
    """五只 ETF 现货 last，键为六位代码。"""
    out = {}
    ticks = etfTickMap if etfTickMap is not None else etfTicks
    daily = etfDailyMap if etfDailyMap is not None else etfDaily
    for code in OPT_UNDERLYING_QMT:
        six = str(code).split(".", 1)[0]
        rec = (ticks or {}).get(code) or {}
        last = _asFloat(rec.get("lastPrice") or rec.get("last"))
        if last is None or last <= 0:
            last = _asFloat(((daily or {}).get(code) or {}).get("close"))
        if last is not None and last > 0:
            out[six] = last
    return out


def shouldApplySzoMinuteTick(existing, minuteTick, asof, meta=None, spot=None) -> bool:
    """创业板主源是 1m：不因旧盘口挡柱。1m close 约 10 倍则尝试 /10，缩不成才丢。"""
    if not isinstance(minuteTick, dict):
        return False
    if optionTickIsTdxFresh(existing):
        return False
    if shouldKeepTickOverMinute(existing, minuteTick, asof):
        return False
    last = minuteTick.get("lastPrice")
    if last is None:
        last = minuteTick.get("last")
    if optionPremiumLooksImplausible(last, meta, spot):
        scaled = scaleTenXOptLast(last, meta, spot)
        if scaled is None:
            return False
        minuteTick["lastPrice"] = scaled
        minuteTick["last"] = scaled
    return True


def overlaySzoMinuteTick(existing, minuteTick) -> dict | None:
    """将 SZO 1 分钟线仅叠加到 TDX 五档之上，绝不把盘口/观测时钟清空。

    1 分钟线只含 latest、累计 volume 与 openInterest，没有买卖五档。若它直接
    替换一条 ``fromTdx`` 记录，会删掉 TDX 的 book/bookTime；随后 T 板会把一张
    实际仍由 TDX 观测到的合约误判为陈旧。这里复用 QMT 字段补丁，只更新其实际
    提供的 last/qty/oi，保留 TDX book 和 bookTime。
    """
    if not isinstance(minuteTick, dict):
        return None
    if (
        isinstance(existing, dict)
        and existing.get("fromTdx")
        and tickHasFiveBand(existing)
    ):
        return optionTickFillQtyFromIncoming(existing, minuteTick)
    return dict(minuteTick)


def dropImplausibleOptLast(packed, meta, spot) -> dict:
    """快照出口：×10 last 缩成权利金；无盘口且缩不成则留空。"""
    if not isinstance(packed, dict):
        return packed
    last = packed.get("last")
    if last is None:
        last = packed.get("lastPrice")
    scaled = scaleTenXOptLast(last, meta, spot)
    lastN = _asFloat(last)
    if tickHasFiveBand(packed):
        if lastN is not None and optionPremiumLooksImplausible(lastN, meta, spot):
            bid = _asFloat(packed.get("bid1"))
            ask = _asFloat(packed.get("ask1"))
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                packed["last"] = mid
                packed["lastPrice"] = mid
            elif scaled is not None:
                packed["last"] = scaled
                packed["lastPrice"] = scaled
        return packed
    if scaled is None:
        packed["last"] = None
        packed["lastPrice"] = None
        packed["fromDaily"] = False
        return packed
    if lastN is not None and abs(float(scaled) - lastN) > 1e-9:
        packed["last"] = scaled
        packed["lastPrice"] = scaled
    return packed


def optionDailyBarLooksStub(bar) -> bool:
    """今开占位柱：停牌旗、无量且昨收为 0。真无成交（昨收>0、suspend=0）不算 stub。"""
    if not isinstance(bar, dict):
        return True
    try:
        flag = int(bar.get("suspendFlag") if bar.get("suspendFlag") is not None else 0)
    except (TypeError, ValueError):
        flag = 0
    if flag == 1:
        return True
    vol = _asFloat(bar.get("volume"))
    pre = _asFloat(bar.get("preClose"))
    if (vol is None or vol <= 0) and (pre is None or pre <= 0):
        return True
    return False


def optionDailyBarFromOneRecord(rec) -> dict | None:
    """日K 一行 → {d, close, volume, oi, preClose, t, suspendFlag}。"""
    if not isinstance(rec, dict):
        return None
    barDay = parseYmdBarDate(
        rec.get("index") or rec.get("time") or rec.get("stime")
    )
    close = _asFloat(rec.get("close"))
    volume = _asFloat(rec.get("volume"))
    oi = _asFloat(rec.get("openInterest"))
    if oi is None:
        oi = _asFloat(rec.get("openInt"))
    pre = _asFloat(rec.get("preClose"))
    if pre is None:
        pre = _asFloat(rec.get("pre_close"))
    try:
        suspendFlag = int(rec.get("suspendFlag") if rec.get("suspendFlag") is not None else 0)
    except (TypeError, ValueError):
        suspendFlag = 0
    return {
        "d": barDay,
        "close": close,
        "open": _asFloat(rec.get("open")),
        "high": _asFloat(rec.get("high")),
        "low": _asFloat(rec.get("low")),
        "volume": volume,
        "oi": oi,
        "preClose": pre,
        "t": rec.get("time") or rec.get("index"),
        "suspendFlag": suspendFlag,
    }


def optionDailyBarFromRecords(records, asof=None) -> dict | None:
    """日K 记录 → 当日非占位柱；asof 为空则取最后一根。"""
    bars = []
    for rec in records or []:
        bar = optionDailyBarFromOneRecord(rec)
        if bar:
            bars.append(bar)
    if not bars:
        return None
    if asof is not None:
        today = [b for b in bars if b.get("d") == asof]
        live = [b for b in today if not optionDailyBarLooksStub(b)]
        if live:
            return live[-1]
        return None
    return bars[-1]


def optionDailyBarFromFrame(frame, asof=None) -> dict | None:
    if frame is None:
        return None
    try:
        if len(frame) <= 0:
            return None
    except TypeError:
        return None
    if hasattr(frame, "reset_index") and hasattr(frame, "to_dict"):
        records = frame.reset_index().to_dict(orient="records")
        for rec in records:
            for key, val in list(rec.items()):
                if hasattr(val, "item"):
                    try:
                        rec[key] = val.item()
                    except Exception:
                        pass
        return optionDailyBarFromRecords(records, asof=asof)
    if isinstance(frame, (list, tuple)):
        return optionDailyBarFromRecords(list(frame), asof=asof)
    if isinstance(frame, dict):
        return optionDailyBarFromRecords([frame], asof=asof)
    return None


def isEtfContinuousSession(now: datetime) -> bool:
    """ETF 连续竞价：[09:15, 15:00)。盘中现货以全推为准，不叠 rt_k。
    rt_k 只有 ETF，没有期权；本开关只影响五只标的现货。"""
    t = now.time()
    return dtTime(9, 15) <= t < dtTime(15, 0)


def etfHasLiveSessionSpot(etfTickMap, asof) -> bool:
    """五只标的全推是否已有当日 last（竞价 last=0 可用买卖中价）。"""
    for code in OPT_UNDERLYING_QMT:
        packed = compactQuoteTick((etfTickMap or {}).get(code), asofDate=asof) or {}
        last = _asFloat(packed.get("last"))
        if last is None:
            last = _asFloat(packed.get("lastPrice"))
        if last is None or last <= 0:
            return False
    return True


def shouldRefreshEtfDailyBars(inWindow: bool, hasLiveEtfSpot: bool) -> bool:
    """直播窗现货走全推，不打 ETF_DAILY；盘前/15:31 后刷日K收盘柱。
    午休冷启动五只都还没有当日 last 时才允许日K兜底。"""
    if not inWindow:
        return True
    return not bool(hasLiveEtfSpot)


def shouldRefreshDailyBars(nowMono: float, lastMono: float, intervalSec: float, inWindow: bool) -> bool:
    """盘中节流日K；盘后每轮都刷，便于收盘棒落地。"""
    if not inWindow:
        return True
    return (nowMono - lastMono) >= float(intervalSec)


def shouldStartPreopenRpc(
    now: datetime,
    timeoutSec: float,
    safetySec: float = 1.0,
) -> bool:
    """按单次 RPC 的最坏超时预算，判断能否在 09:15 前安全启动。"""
    openAt = datetime.combine(now.date(), dtTime(9, 15))
    if now >= openAt:
        return False
    budget = max(0.0, float(timeoutSec)) + max(0.0, float(safetySec))
    return now + timedelta(seconds=budget) <= openAt


def shouldDownloadOptionTicks(now: datetime) -> bool:
    """仅在 RPC 最坏超时仍能于 09:15 前结束时灌 tick。

    ``download_history_data2(period=tick)`` 会在整个请求期间持有共享
    ``_rpcLock``。若临近 09:15 仍允许启动，120 秒超时足以跨过开盘，
    令 whole-quote watchdog 连续拿不到锁。按配置超时倒推截止点，保留
    开盘订阅恢复所需的不可抢占窗口。
    """
    return shouldStartPreopenRpc(now, OPT_TICK_DL_TIMEOUT_SEC)


def shouldDownloadOptionTickGaps(now: datetime, needN: int) -> bool:
    """直播窗只灌当日新挂缺口；数量过大视为冷启动空缓存，不灌。"""
    n = int(needN or 0)
    if n <= 0 or n > OPT_TICK_GAP_MAX_N:
        return False
    return isOptionPollWindow(now)


def optionLiveTickGapCodes(need, listedToday) -> list:
    """直播窗缺口 = 当日新挂 ∩ 无当日 tick；沪市 SHO 优先，避免先灌满 SZO。"""
    allow = {str(c).strip() for c in (listedToday or []) if str(c).strip()}
    if not allow:
        return []
    picked = [c for c in (need or []) if str(c).strip() in allow]
    sho = [c for c in picked if str(c).upper().endswith(".SHO")]
    rest = [c for c in picked if not str(c).upper().endswith(".SHO")]
    return sho + rest


def rememberOptionChainAdds(added, asof) -> None:
    """拉链新增记为当日新挂，供直播窗小片灌 tick。跨日清空。"""
    global _optionListedToday, _optionListedTodayAsOf
    day = asof if isinstance(asof, date) else date.today()
    if _optionListedTodayAsOf != day:
        _optionListedToday = set()
        _optionListedTodayAsOf = day
    for code in added or []:
        text = str(code or "").strip()
        if text:
            _optionListedToday.add(text)


def optionListedTodayCodes(asof) -> list:
    day = asof if isinstance(asof, date) else date.today()
    if _optionListedTodayAsOf != day:
        return []
    return list(_optionListedToday)


def optionTickGapDownloadChunk(need, chunkSize: int | None = None) -> list:
    """缺口灌库片：默认 OPT_TICK_DL_CHUNK=15，禁止整链一次打满 ROUTER。"""
    size = int(chunkSize) if chunkSize else OPT_TICK_DL_CHUNK
    if size < 1:
        size = OPT_TICK_DL_CHUNK
    return list(need or [])[:size]


def mergeOptionPollCodes(liveCodes, extra) -> list:
    out = []
    seen = set()
    for code in list(liveCodes or []) + list(extra or []):
        text = str(code or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def optionSzoCodes(codes) -> list:
    """创业板期权 .SZO，顺序与 codes 一致。"""
    out = []
    for code in codes or []:
        text = str(code).strip()
        if text.upper().endswith(".SZO"):
            out.append(text)
    return out


def optionShoCodes(codes) -> list:
    """上交所期权 .SHO，顺序与 codes 一致。"""
    out = []
    for code in codes or []:
        text = str(code).strip()
        if text.upper().endswith(".SHO"):
            out.append(text)
    return out


def optionDailySubscribeCodes(codes) -> list:
    """创业板 .SZO 列表（闸门/健康检查用）。tick 订阅宇宙见 optionQuoteSubscribeCodes。"""
    out: list[str] = []
    seen: set[str] = set()
    for code in codes or []:
        text = str(code).strip()
        if not text.upper().endswith(".SZO"):
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def optionQuoteSubscribeCodes(codes, metaMap=None, pollOnly=None) -> list:
    """subscribe_quote(tick) 宇宙：科创+中证500+沪深300 全订（约 418≤500）。

    50ETF 整链走轮询，不进 500 配额。无标的元数据的合约也不订，避免 510050 混入。
    深市不订（实测 0 回调）。heal 放弃的合约改 poll-only，不占句柄。
    """
    sho = optionShoCodes(codes)
    metaMap = metaMap or {}
    mixed = interleaveOptionCodesByUnderlying(sho, metaMap) if metaMap else list(sho)
    want = frozenset(OPT_QUOTE_SUB_UNDLS)
    skip = {str(c).strip().upper() for c in (pollOnly or []) if str(c).strip()}
    out = []
    for code in mixed:
        if undlSixFromMeta(metaMap.get(code)) not in want:
            continue
        if str(code).strip().upper() in skip:
            continue
        out.append(code)
    return out


def optionCacheMissCodes(codes, cached, maxAgeSec: float) -> list:
    """缓存没有、或 ageSec 超过阈值的合约。"""
    cached = cached or {}
    miss = []
    for code in codes or []:
        rec = cached.get(code)
        if rec is None:
            rec = cached.get(str(code).strip().upper())
        if not isinstance(rec, dict):
            miss.append(code)
            continue
        age = rec.get("ageSec")
        try:
            tooOld = age is None or float(age) > float(maxAgeSec)
        except (TypeError, ValueError):
            tooOld = True
        if tooOld:
            miss.append(code)
    return miss


def packOptionPollSlices(groups, chunkSize: int) -> list:
    """把标的组打进 ≤chunkSize 的片；50+300 合成一片，单链超限再切。"""
    size = int(chunkSize) if chunkSize else 280
    if size < 1:
        size = 280
    slices = []
    current = []
    for group in groups or []:
        if not group:
            continue
        if len(group) > size:
            if current:
                slices.append(current)
                current = []
            slices.extend(chunkCodes(group, size))
            continue
        if current and len(current) + len(group) > size:
            slices.append(current)
            current = []
        current.extend(group)
    if current:
        slices.append(current)
    return slices


def shouldSyncOptionQuoteSub(
    asof,
    lastAsof,
    wanted,
    owned,
    maxOwned: int = 500,
    lastSyncMono: float = 0.0,
    nowMono: float = 0.0,
    healSec: float = 0.0,
    optionCallbackEmpty: bool = False,
) -> bool:
    """未订满或日切必须 sync；期权 callback 归零时冻结换订，避免加重恢复。"""
    if optionCallbackEmpty:
        return False
    if optionQuoteOwnedExtras(wanted, owned):
        return True
    if not optionQuoteSubIsHealthy(wanted, owned, maxOwned):
        return True
    if lastAsof is not None and asof != lastAsof:
        return True
    if float(healSec or 0) > 0 and float(nowMono or 0) > 0 and float(lastSyncMono or 0) > 0:
        if (float(nowMono) - float(lastSyncMono)) >= float(healSec):
            return True
    return False


def shouldForceQuoteSubResync(
    cacheHitN: int,
    emptyHits: int,
    needEmpty: int = 3,
    hadCacheHits: bool = False,
    isSubFull: bool = False,
    inAuction: bool = False,
    sinceForceSec: float | None = None,
    forceCooldownSec: float = 90.0,
    cachedN: int = 0,
    degradedStreak: int = 0,
    degradeRatio: float = OPT_SUB_DEGRADE_RATIO,
    degradeNeed: int = OPT_SUB_DEGRADE_STREAK,
    cacheAuthoritative: bool = True,
    healthDegraded: bool = True,
) -> bool:
    """仅连续竞价、订满，且(连续空缓存 或 推送速率退化)且过冷却才重订。

    午休空缓存是常态，不得 force。

    **推送退化判据（2026-09-11 新增）**：原实现只在 `cacheHitN == 0` 时触发，
    但实测冻结现场是 `hit=16~48`（远低于正常 ~140）—— 非零就永不重订，
    导致午休→下午盘边界后约 10 分钟整链陈旧（T 板整列变灰）。
    改为同时看**命中率**：`hit/cachedN < degradeRatio` 连续 N 次即视为退化。
    """
    if not inAuction:
        return False
    if not isSubFull:
        return False
    if not cacheAuthoritative or not healthDegraded:
        return False
    if sinceForceSec is not None and float(sinceForceSec) < float(forceCooldownSec or 0):
        return False
    # 退化：命中率过低（且样本足够）
    if int(cachedN or 0) >= 100 and int(cacheHitN or 0) >= 0:
        ratio = float(cacheHitN) / float(cachedN)
        if ratio < float(degradeRatio) and int(degradedStreak or 0) >= int(degradeNeed):
            return True
    # 非退化但仍非零命中 → 无需重订
    if int(cacheHitN or 0) > 0:
        return False
    return int(emptyHits or 0) >= max(1, int(needEmpty or 1))


def shouldCountOptionQuoteCacheEmpty(
    outcome: str,
    inAuction: bool,
    *,
    sinceQtySec: float | None = None,
    postQtyGraceSec: float = OPT_QUOTE_CACHE_POST_QTY_GRACE_SEC,
) -> bool:
    """仅把竞价期内、脱离 OPT_QTY 短暂争用窗的权威空响应计入死推送。"""
    if not inAuction or str(outcome or "") != "authoritative_empty":
        return False
    if sinceQtySec is not None and float(sinceQtySec) < max(
        0.0, float(postQtyGraceSec or 0.0)
    ):
        return False
    return True


def shouldDeferOptionQuoteCacheRecheck(
    outcome: str,
    inAuction: bool,
    *,
    sinceQtySec: float | None,
    postQtyGraceSec: float = OPT_QUOTE_CACHE_POST_QTY_GRACE_SEC,
) -> bool:
    """OPT_QTY 后短窗内先停一轮重请求，让缓存读获得无争用复查机会。"""
    if not inAuction or sinceQtySec is None:
        return False
    if str(outcome or "") not in {
        "authoritative_empty", "busy", "timeout", "error", "transport_unknown",
    }:
        return False
    return float(sinceQtySec) < max(0.0, float(postQtyGraceSec or 0.0))


def shouldEnterOptionPushFallback(
    ownedN,
    wantedN,
    cachedN,
    inAuction: bool,
    forcePending: bool,
    *,
    sinceForceSec: float | None,
    confirmSec: float = OPT_QUOTE_SUB_FORCE_CONFIRM_SEC,
) -> bool:
    """一次受控重订后仍无新 callback，才认定订阅推送暂时失效。

    已订句柄数量不能证明 QMT callback 仍在推进。此判据只打开本地熔断：
    停止每 90 秒的无效 drop/add，保留既有有界 tick/TDX 回退；一旦重新看到
    新鲜缓存，调用方立即退出降级态。
    """
    if not inAuction or not forcePending:
        return False
    if int(wantedN or 0) <= 0 or int(ownedN or 0) < int(wantedN or 0):
        return False
    if int(cachedN or 0) > 0:
        return False
    if sinceForceSec is None:
        return False
    try:
        return float(sinceForceSec) >= max(0.0, float(confirmSec))
    except (TypeError, ValueError):
        return False


def shouldExitOptionPushFallback(
    cachedN,
    *,
    sinceRecoverySec: float | None,
    confirmSec: float = OPT_QUOTE_SUB_RECOVER_CONFIRM_SEC,
) -> bool:
    """仅连续看到新鲜 callback 一个确认窗后，才解除 dead-push 回退态。"""
    if int(cachedN or 0) <= 0 or sinceRecoverySec is None:
        return False
    try:
        return float(sinceRecoverySec) >= max(0.0, float(confirmSec))
    except (TypeError, ValueError):
        return False


def pingRevisionMatches(payload, expected: str) -> bool:
    """桥接 ping.rpc_revision 必须与本脚本期望值完全一致。"""
    rev = str((payload or {}).get("rpc_revision") or "").strip()
    return bool(expected) and rev == str(expected or "").strip()


def shouldRefuseOptionSubForRevision(revisionOk: bool) -> bool:
    return not bool(revisionOk)


def shouldHealUncachedQuoteSub(
    ownedN,
    wantedN,
    cachedN,
    inAuction: bool,
    sinceHealSec: float | None = None,
    cooldownSec: float = OPT_QUOTE_SUB_HEAL_SEC,
    healExhausted: bool = False,
) -> bool:
    """连续竞价、订满、缓存半残：只重订未进回调的合约。整本空走 force。"""
    if healExhausted:
        return False
    if not inAuction:
        return False
    wanted = int(wantedN or 0)
    owned = int(ownedN or 0)
    cached = int(cachedN or 0)
    if wanted <= 0 or owned < wanted:
        return False
    if cached <= 0 or cached >= wanted:
        return False
    if sinceHealSec is not None and float(sinceHealSec) < float(cooldownSec or 0):
        return False
    return True


def healUncachedMissKey(dropped, added=None) -> tuple:
    """本轮 heal 动过的代码指纹。优先退订名单。"""
    codes = [str(c).strip().upper() for c in (dropped or []) if str(c).strip()]
    if not codes:
        codes = [str(c).strip().upper() for c in (added or []) if str(c).strip()]
    return tuple(sorted(set(codes)))


def noteHealUncachedOutcome(
    prevCached,
    newCached,
    dropped,
    added=None,
    prevKey=(),
    prevFailN=0,
    maxFail: int = OPT_QUOTE_HEAL_FAIL_STOP,
):
    """同一批 miss 且 cached 未增：累加失败。返回 (key, failN, pollOnly)。"""
    key = healUncachedMissKey(dropped, added)
    try:
        prevC = int(prevCached or 0)
    except (TypeError, ValueError):
        prevC = 0
    try:
        newC = int(newCached or 0)
    except (TypeError, ValueError):
        newC = 0
    if not key or newC > prevC:
        return (), 0, []
    try:
        prevK = tuple(prevKey or ())
    except TypeError:
        prevK = ()
    failN = int(prevFailN or 0) + 1 if key == prevK else 1
    try:
        stopAt = int(maxFail)
    except (TypeError, ValueError):
        stopAt = OPT_QUOTE_HEAL_FAIL_STOP
    if stopAt < 1:
        stopAt = 1
    pollOnly = list(key) if failN >= stopAt else []
    return key, failN, pollOnly


def optionQuoteOwnedExtras(wanted, owned) -> list:
    """已订但不在 wanted 里的代码（poll-only 剔除后待退订）。"""
    wantSet = {str(c).strip().upper() for c in (wanted or []) if str(c).strip()}
    extras = []
    seen = set()
    for raw in owned or []:
        text = str(raw).strip().upper()
        if not text or text in seen:
            continue
        seen.add(text)
        if text not in wantSet:
            extras.append(text)
    return extras


def shouldSkipHeavyUntilQuoteSubFull(wanted, owned, maxOwned: int = 500) -> bool:
    """未订满时禁止整链 1d/tick，避免盘前灌库堵死加订。"""
    return not optionQuoteSubIsHealthy(wanted, owned, maxOwned)


def isOptHealthDeadPush(ownedN, wantedN, cachedN, inAuction: bool) -> bool:
    """连续竞价、订满、回调缓存为空：假订满/死推送。"""
    if not inAuction:
        return False
    wanted = int(wantedN or 0)
    if wanted <= 0:
        return False
    return int(ownedN or 0) == wanted and int(cachedN or 0) == 0


def maybeLogOptHealthDeadPush(
    logger,
    ownedN,
    wantedN,
    cachedN,
    inAuction: bool,
    lastLogMono: float,
    nowMono: float,
    cooldownSec: float = OPT_HEALTH_DEAD_PUSH_LOG_SEC,
) -> float:
    if not isOptHealthDeadPush(ownedN, wantedN, cachedN, inAuction):
        return lastLogMono
    if lastLogMono > 0 and (float(nowMono) - float(lastLogMono)) < float(cooldownSec or 0):
        return lastLogMono
    logger.warning(
        "OPT_HEALTH dead_push owned=%d wanted=%d cached=%d",
        int(ownedN or 0),
        int(wantedN or 0),
        int(cachedN or 0),
    )
    return float(nowMono)


def applyBridgePingPayload(payload) -> bool:
    """写入全局 revision 门闩。返回是否允许期权订阅。"""
    global _bridgeRevisionOk, _bridgeRevision, _optSubRevisionRefuseLogged
    data = payload if isinstance(payload, dict) else {}
    _bridgeRevision = str(data.get("rpc_revision") or "").strip()
    _bridgeRevisionOk = pingRevisionMatches(data, EXPECTED_RPC_REVISION)
    if _bridgeRevisionOk:
        _optSubRevisionRefuseLogged = False
    return _bridgeRevisionOk


def isWholeQuoteHealthyForOptDl(
    uniqueN,
    silenceSec,
    uniqueMin: int = OPT_LIVE_DL_UNIQUE_MIN,
    silenceMax=None,
) -> bool:
    """全推已站住才灌期权库；冷启动/假活时灌库会再饿死回调。"""
    if silenceMax is None:
        silenceMax = OPT_YIELD_SILENCE_SEC
    return int(uniqueN or 0) >= int(uniqueMin) and float(silenceSec or 0.0) < float(
        silenceMax
    )


def optionTickLooksUsable(tick, asof) -> bool:
    """当日 last>0 或真五档。仅有今日时间戳/昨收/持仓不算覆盖。"""
    if not isinstance(tick, dict) or asof is None:
        return False
    tickDay = decodeQuoteTickDate(
        tick.get("time") or tick.get("stime") or tick.get("t")
    )
    if tickDay is not None and tickDay != asof:
        return False
    last = _asFloat(tick.get("lastPrice"))
    if last is None:
        last = _asFloat(tick.get("last"))
    if last is not None and last > 0:
        return True
    return tickHasFiveBand(tick)


def optionTickIsTdxFresh(tick, now=None, maxAgeSec: float | None = None) -> bool:
    """通达信刚写入且未过墙钟阈值，QMT 1m/轮询不得回盖。

    空盘口（无 last、无买卖一）不算 fresh，避免开盘竞价清空后锁死 QMT 回补。
    """
    if not isinstance(tick, dict) or not tick.get("fromTdx"):
        return False
    if not optionTickLooksUsable(tick, date.today()):
        return False
    cap = TDX_OPT_MAX_AGE_SEC if maxAgeSec is None else float(maxAgeSec)
    age = optionTickAgeSec(tick, now)
    if age is None:
        return False
    return 0.0 <= float(age) <= cap


def tdxOptCodeAllowed(code, rec=None, meta=None, existing=None) -> bool:
    """合并通达信期权：默认五链；SZO 一律放行（创业板）。

    2026-09-11：QMT 订阅链（`QMT_LED_UNDLS`）在 **QMT 新鲜且处于连续竞价时段**
    时由 QMT 主导，这些链的 SHO 合约不接受 TDX 覆盖。判据三条：
      1) 时段：非竞价时段一律放行（午休/盘后 QMT 自身也陈旧）；
      2) 新鲜度：`existing`（当前 optionTicks 记录）陈旧时放行 → TDX 接管；
      3) 标的：非 QMT 主导链一律放行。
    依据实测三方对照（`scripts/compare_quote_sources.py`）：
      QMT 新鲜时滞后 0.2~3.0s（TDX 2.3~122s）→ QMT 主导；
      QMT 冻结时（实测下午盘卡死 155s+）→ 立即让 TDX 兜底。
    """
    text = str(code or "").strip().upper()
    if text.endswith(".SZO"):
        return True
    undl = ""
    if isinstance(rec, dict):
        undl = str(rec.get("undl") or "").split(".", 1)[0].strip()
    if not undl:
        undl = undlSixFromMeta(meta)
    if text.endswith(".SHO") and qmtLedChain(undl, existing):
        # 整票 `time` 可能刚被 TDX 五档刷新，不能拿它证明 QMT 的 last/vol
        # 也是新鲜的。只要这两个 QMT 负责字段之一超时，就放行 TDX 做字段接管；
        # OI 不在此判据内，TDX 没有可信 OI，不能因它超时而制造伪修复。
        if not qmtLedLastOrQtyStale(existing):
            return False
    return undl in TDX_OPT_UNDLS


def isTdxServedOptionChain(code, meta=None) -> bool:
    """该期权合约是否由通达信供给（不应进 QMT 期权快照）。

    分工（2026-09-11 与用户确认）：510050 / 159915 走通达信；
    510300 / 510500 / 588000 走 QMT 订阅（见 `OPT_QMT_TICK_UNDERLYINGS`）。
    SZO（深市创业板）恒由通达信供给（无 tick 回调）。
    """
    text = str(code or "").strip().upper()
    if text.endswith(".SZO"):
        return True
    six = str(meta.get("undl") or "").split(".", 1)[0] if isinstance(meta, dict) else ""
    if not six:
        six = undlSixFromMeta(meta)
    if not six:
        return False
    return six not in OPT_QMT_TICK_UNDERLYINGS and six in (
        "510050",
        "159915",
    )


def isTickOlderThan(tick, maxAgeSec: float, now=None) -> bool:
    """记录相对**当前墙钟**是否已超过 maxAgeSec（区别于 isStaleIncomingTick）。

    `isStaleIncomingTick` 判的是「tick 与 existing 的时间**差**」（用于防回盖）；
    这里判的是「记录本身有多旧」（用于决定是否让备源接管）。
    时间戳不可解析 → 视为陈旧（fail-closed）。
    """
    ms = optionTickTimeMs(tick)
    if ms <= 0:
        return True
    wall = now if isinstance(now, datetime) else datetime.now()
    try:
        age = wall.timestamp() - ms / 1000.0
    except (OSError, OverflowError, ValueError):
        return True
    return age > float(maxAgeSec)


def qmtLedChain(undl, existing=None) -> bool:
    """该标的是否**此刻**由 QMT 订阅主导（TDX 不覆盖盘口）。

    三方对照实测（2026-09-11 10:46，510500）：
      QMT 订阅 0.2~3.0s / QMT 轮询 0.2~3.0s / 通达信 2.3~122s，
      QMT tick 字段含 bidPrice/askPrice/bidVol/askVol 五档 + openInt。

    **只在连续竞价时段**让 QMT 独占：实测 12:33（午休）QMT 盘口 46/158、
    滞后 3831s —— 非竞价时段 QMT 本身也陈旧，此时必须让 TDX 兜底，
    否则午休/盘后整链无盘口（2026-09-11 引入并当天修正的回归）。

    **且要求 QMT 自身新鲜**：2026-09-11 下午盘实测 QMT 期权 tick 卡死在
    13:02:48（滞后 155s）而 TDX 仅 2~35s。若只看时段，盘中会因 QMT 冻结而
    整链卡住（实测 daqmt 快照 age≈5144s）。故新增 `existing` 判据：
    QMT tick 陈旧时立刻让 TDX 接管（降级/自愈），恢复后自动切回。

    `DAQMT_QMT_LED_UNDLS=""` 可整体关闭；`DAQMT_QMT_LED_ENABLE=0` 亦可。
    """
    if not QMT_LED_ENABLE:
        return False
    if not isWatchdogWindow(datetime.now()):
        return False
    six = str(undl or "").strip().split(".", 1)[0]
    if not six or six not in QMT_LED_UNDLS:
        return False
    if isinstance(existing, dict) and isTickOlderThan(
        existing, QMT_LED_MAX_STALE_SEC
    ):
        return False
    return True


def qmtLedLastOrQtyStale(existing, now=None) -> bool:
    """QMT 主导链的 last/qty 是否已超时，供 TDX 字段级让位使用。

    不读整票 `time`：它可由 TDX 的新盘口推进，无法反映 QMT 最后一次给
    latest/volume 的时间。只对已明确标为 QMT 的字段触发，让历史旧快照继续按
    原有整票门控走，避免无来源数据被意外切换。
    """
    if not isinstance(existing, dict):
        return False
    wall = now if isinstance(now, datetime) else datetime.now()
    cutoffMs = wall.timestamp() * 1000.0 - QMT_LED_MAX_STALE_SEC * 1000.0
    for field, sourceKey in (("last", "lastSource"), ("qty", "qtySource")):
        if str(existing.get(sourceKey) or "").strip().lower() != "qmt":
            continue
        if optionTickFieldTimeMs(existing, field) < cutoffMs:
            return True
    return False


def optionShoPollSkipTdxCovered(codes, metaMap=None) -> list:
    """可选：跳过通达信已覆盖链的 OPT_CHUNK。

    默认不跳过：TQ 无持仓字段且 Volume 常滞后，需 get_option_ticks 经
    optionTickFillQtyFromIncoming 补 vol/oi（盘口仍由 TDX 锁住不回盖）。
    开盘若 chunk 超时风暴，可设 DAQMT_OPT_SKIP_TDX_CHUNK=1 恢复旧行为。
    """
    codesOut = [str(c).strip() for c in (codes or []) if str(c).strip()]
    if os.getenv("DAQMT_OPT_SKIP_TDX_CHUNK", "0") == "1":
        if not TDX_OPT_ENABLE:
            return codesOut
        metaMap = metaMap or {}
        out = []
        for text in codesOut:
            undl = undlSixFromMeta(metaMap.get(text))
            if undl and undl in TDX_OPT_UNDLS:
                continue
            out.append(text)
        return out
    return codesOut


def fetchTdxOptSnapshot(url: str = TDX_OPT_SNAP_URL, timeoutSec: float | None = None) -> dict:
    """读通达信喂数进程；失败返回空，由 QMT 兜底。"""
    timeout = TDX_OPT_FETCH_TIMEOUT_SEC if timeoutSec is None else float(timeoutSec)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict) or not data.get("ok"):
        return {}
    opt = data.get("opt")
    return opt if isinstance(opt, dict) else {}


def mergeTdxOptionTicks(tdxOpt, metaMap=None, *, forceTdx: bool = False) -> int:
    """把 15581 快照写入 optionTicks；默认五链（两链主供 + 三链兜底）。

    空盘口（无 last/买卖一）不落库，避免每秒刷墙钟把 fromTdx fresh 锁死、挡住 QMT。
    `forceTdx=True` 仅供 QMT RPC 桥接尚未就绪的降级线程使用：此时不能让
    QMT 主导链的常规门控把唯一健康源也拒掉。
    """
    if not TDX_OPT_ENABLE or not tdxOpt:
        return 0
    asof = date.today()
    metaMap = metaMap or {}
    # 本轮合并的观测时刻：两种落盘形状共用同一个拍点，
    # 使「同一轮 TDX 喂数」的字段时钟严格一致（LESSON-020）。
    observedMs = time.time() * 1000.0
    ok = 0
    with _cacheLock:
        for code, rec in tdxOpt.items():
            if not isinstance(rec, dict):
                continue
            # 传 existing：QMT tick 陈旧时放行 TDX 接管（降级/自愈）
            if (not forceTdx) and not tdxOptCodeAllowed(
                code, rec, metaMap.get(code), optionTicks.get(code)
            ):
                continue
            incoming = dict(rec)
            incoming["fromTdx"] = True
            optionTickStampFieldSources(incoming, "tdx")
            if not optionTickLooksUsable(incoming, asof):
                continue
            existing = optionTicks.get(code)
            if not optionTickShouldReplace(existing, incoming, asof):
                continue
            # QMT 主导链的回调若超过让位阈值，TDX 的本轮实测值可接管
            # last/volume；OI 仍由 merge 函数保留 QMT 值与其真实时钟。
            merged = optionTickMergeKeepQty(
                existing,
                incoming,
                qmtFreshBeforeMs=observedMs - QMT_LED_MAX_STALE_SEC * 1000.0,
            )
            if not optionTickPayloadChanged(existing, merged):
                # 指纹只比较数值与整票时间，不包含字段来源。过期 QMT 与 TDX 恰好
                # 给出相同 last/vol 时，仍必须提交本次「来源交接」，否则数值虽相同、
                # 字段时钟却继续停在陈旧 QMT，页面会错误地长期标灰。
                sourceChanged = any(
                    str(existing.get(key) or "").strip().lower() == "qmt"
                    and str(merged.get(key) or "").strip().lower() != "qmt"
                    for key in ("lastSource", "qtySource", "oiSource", "bookSource")
                ) if isinstance(existing, dict) else False
                if sourceChanged:
                    optionTickStampTdxFieldClocks(merged, existing, observedMs)
                    optionTickStampChangedFieldClocks(merged, existing, observedMs)
                    optionTicks[code] = merged
                    ok += 1
                    continue
                # 盘口值未变（指纹只差 time 或完全相同）时，**仍要把盘口时钟推进**
                # 到 incoming 的时间戳 —— 否则该记录会永久被判陈旧。
                #
                # 缺陷现场（2026-09-14 方案 A 部署后实测，
                # `scripts/probe_tdx_drop_real.py`）：`10011545.SHO` TDX 侧 323s、
                # daqmt 侧 **12848s**（差 3.5 小时）。复现显示
                # `optionTickShouldReplace=True` 但 `optionTickPayloadChanged=False`
                # 且指纹差异位**只有 time** → 整 tick 被丢弃。
                # 原实现的补时钟分支被 `and not existing.get("fromTdx")` 限制，
                # 而这类记录**早已是 fromTdx=True** → 分支永不执行 → 时间戳冻结，
                # 即使 TDX 每 3s 都在送新时间戳。T 板因此把它们一直标成陈旧。
                if isinstance(existing, dict) and incoming.get("fromTdx"):
                    inMs = optionTickTimeMs(incoming)
                    curMs = optionTickTimeMs(existing)
                    # 两种都要处理：
                    #  ① 原本不是 fromTdx → 补标记（既有用例
                    #     testMergePatchesFromTdxWhenBookUnchanged 的场景，
                    #     此时时间戳可能相同，不能只看"更新"）
                    #  ② 已是 fromTdx 但时间戳更旧 → **前进**到 incoming
                    #     （本日修的缺陷场景）
                    # 统一"不倒退"：incoming 时间戳更旧且已有 fromTdx 时跳过。
                    newlyTdx = not existing.get("fromTdx")
                    advancing = inMs > curMs
                    # TDX 的 `time` 是源端最后市场事件时间，缓存被强制刷新后若
                    # 行情数值未变，它可以持续不动；但本进程仍确实重新观测到了
                    # TDX 负责的 latest/book。故 common time 只在事件前进时推进，
                    # 字段的 observed clock 则每次有效重观测都可前进。
                    patched = dict(existing)
                    patched["fromTdx"] = True
                    if advancing or (inMs > 0 and curMs <= 0):
                        patched["time"] = incoming.get("time")
                        if incoming.get("t") is not None:
                            patched["t"] = incoming.get("t")
                    # 该 helper 仅更新当前由 TDX 负责的 last/book，不会用 TDX
                    # 为 QMT 的 latest/volume/OI 伪造新鲜度。
                    optionTickStampTdxFieldClocks(patched, existing, observedMs)
                    optionTickStampChangedFieldClocks(patched, existing, observedMs)
                    clocksAdvanced = any(
                        patched.get(key) != existing.get(key)
                        for key in (
                            "time", "t", "lastTime", "bookTime",
                            "lastChangedTime", "bookChangedTime",
                        )
                    )
                    if newlyTdx or advancing or clocksAdvanced:
                        optionTicks[code] = patched
                        ok += 1
                continue
            optionTickStampTdxFieldClocks(merged, existing, observedMs)
            optionTickStampChangedFieldClocks(merged, existing, observedMs)
            optionTicks[code] = merged
            ok += 1
        if ok:
            optionStats["lastWall"] = datetime.now()
            optionStats["lastPushMono"] = time.monotonic()
    if ok:
        invalidateQuoteSnapshotJson()
    return ok


def optionTickShouldReplace(existing, incoming, asof, now=None) -> bool:
    """空 tick 不得盖掉已有可用盘口。通达信可用盘口不被 QMT 整票回盖。

    说明：TDX 内容未变时不重盖墙钟，age 会超过 TDX_OPT_MAX_AGE_SEC。
    若仅按「新鲜」解锁，QMT 会整票替换且不打 qtyFromQmt，随后 TDX 合并
    又用滞后 last/vol 回盖。量仓/最新只走 optionTickFillQtyFromIncoming。

    保留原因（2026-09-14 调研结论，勿再改）：
    曾怀疑"TDX 优先级压制新鲜 QMT"是今日新鲜度劣化的根因，拟把判据换成
    `optionTickIsTdxFresh`（带年龄上限）—— **实测证伪**：被压制的 409 个合约里，
    快照的 last/vol/oi 与桥**完全一致**（FillQty 已把 QMT 值写进去了），
    真正不推进的是 common `time`/`t`（TDX 盘口时钟），即
    `optionTickFillQtyFromIncoming` 的**刻意设计**（防止抹掉 TDX 盘口时间）。
    换成 IsTdxFresh 只会让 QMT 整票替换、反而丢掉 TDX 五档，
    并直接破坏 `testStaleTdxBookStillBlocksQmtReplace` 等既有回归。
    """
    if not isinstance(incoming, dict):
        return False
    if (
        isinstance(existing, dict)
        and existing.get("fromTdx")
        and not incoming.get("fromTdx")
        and optionTickLooksUsable(existing, asof)
    ):
        return False
    if optionTickLooksUsable(incoming, asof):
        return True
    if not isinstance(existing, dict):
        return True
    return not optionTickLooksUsable(existing, asof)


def optionTickFingerprint(tick) -> tuple:
    """last/买一/卖一/量/仓/时间。"""
    last, bid1, ask1, vol, oi = optionTickQuoteFingerprint(tick)
    return (last, bid1, ask1, vol, oi, round(optionTickTimeMs(tick), 0))


def _optionTickTimeMsRaw(raw) -> float:
    """归一化到毫秒；不可解析返回 0.0。与 optionTickTimeMs 同规则但不读兄弟键。"""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if val > 1e12:
        return val
    if val > 1e9:
        return val * 1000.0
    return 0.0


# 字段级时钟键：与 rt_monitor/services/opt_t_board.py 的 _OPT_TICK_*_TIME_KEYS 对齐。
# 语义：字段自己的行情日；缺该键才回退 common time/t；键存在但值无效 → 判为不可信（fail-closed）。
OPT_TICK_FIELD_TIME_KEYS = {
    "last": ("lastTime", "last_time"),
    "qty": ("qtyTime", "qty_time"),
    "oi": ("oiTime", "oi_time"),
    "book": ("bookTime", "book_time"),
}
# 与观测时钟分开：仅字段数值实际变动才推进。缺失时下游只能把它显示为未知，
# 绝不可用观测时钟伪造“刚变动”。
OPT_TICK_FIELD_CHANGED_TIME_KEYS = {
    "last": ("lastChangedTime", "last_changed_time"),
    "qty": ("qtyChangedTime", "qty_changed_time"),
    "oi": ("oiChangedTime", "oi_changed_time"),
    "book": ("bookChangedTime", "book_changed_time"),
}


def optionTickFieldTimeMs(tick, field: str) -> float:
    """字段自己的时钟（毫秒）。该字段有专属键则只认它；无专属键才回退 common time/t。

    与 opt_t_board._optTickFieldDate 同构：显式键存在但不可解析时返回 0，
    调用方据此判该字段不可信，绝不静默回退到别的字段时间。
    """
    if not isinstance(tick, dict):
        return 0.0
    keys = OPT_TICK_FIELD_TIME_KEYS.get(str(field or "").strip())
    if not keys:
        return optionTickTimeMs(tick)
    hasExplicit = any(key in tick for key in keys)
    for key in keys:
        val = _optionTickTimeMsRaw(tick.get(key))
        if val > 0:
            return val
    if hasExplicit:
        return 0.0
    return optionTickTimeMs(tick)


def optionTickSetFieldTime(out: dict, field: str, timeMs) -> None:
    """写入字段专属时钟；timeMs<=0 时写 0 占位（显式标记不可信，禁止回退 common）。"""
    keys = OPT_TICK_FIELD_TIME_KEYS.get(str(field or "").strip())
    if not keys or not isinstance(out, dict):
        return
    val = _optionTickTimeMsRaw(timeMs)
    out[keys[0]] = val if val > 0 else 0


def optionTickFieldChangedTimeMs(tick, field: str) -> float:
    """字段最后数值变动时间；显式但无效时 fail-closed 返回 0。"""
    if not isinstance(tick, dict):
        return 0.0
    keys = OPT_TICK_FIELD_CHANGED_TIME_KEYS.get(str(field or "").strip())
    if not keys:
        return 0.0
    for key in keys:
        val = _optionTickTimeMsRaw(tick.get(key))
        if val > 0:
            return val
    return 0.0


def optionTickSetFieldChangedTime(out: dict, field: str, timeMs) -> None:
    """写字段最后变动时钟；只由实际数值变化调用。"""
    keys = OPT_TICK_FIELD_CHANGED_TIME_KEYS.get(str(field or "").strip())
    if not keys or not isinstance(out, dict):
        return
    val = _optionTickTimeMsRaw(timeMs)
    out[keys[0]] = val if val > 0 else 0


def optionTickFieldValue(tick, field: str):
    """用于变动时钟的稳定字段值；盘口使用完整五档价量。"""
    rec = tick if isinstance(tick, dict) else {}
    last, bid1, ask1, vol, oi = optionTickQuoteFingerprint(rec)
    if field == "last":
        return last
    if field == "qty":
        return vol
    if field == "oi":
        return oi
    if field == "book":
        deepBook = optionTickBookFingerprint(rec)
        if bid1 is None and ask1 is None and not any(deepBook):
            return None
        return (bid1, ask1, deepBook)
    return None


def optionTickCarryChangedTime(out, existing, field: str) -> None:
    """保留旧字段值时同步保留其变动时钟，禁止新源墙钟给旧值背书。"""
    if not isinstance(out, dict) or not isinstance(existing, dict):
        return
    keys = OPT_TICK_FIELD_CHANGED_TIME_KEYS.get(field) or ()
    src = optionTickFieldChangedTimeMs(existing, field)
    if src <= 0:
        return
    for key in keys:
        out[key] = src


def optionTickStampChangedFieldClocks(out, existing, fallbackMs) -> None:
    """维护变动时间：相同数值只保留旧时钟，不把“再次观察”当成“发生变化”。"""
    if not isinstance(out, dict):
        return
    old = existing if isinstance(existing, dict) else {}
    for field in ("last", "qty", "oi", "book"):
        value = optionTickFieldValue(out, field)
        oldValue = optionTickFieldValue(old, field)
        if value is None:
            optionTickCarryChangedTime(out, old, field)
            continue
        changedAt = optionTickFieldChangedTimeMs(old, field)
        if changedAt > 0 and value == oldValue:
            optionTickCarryChangedTime(out, old, field)
            continue
        observed = optionTickFieldTimeMs(out, field)
        optionTickSetFieldChangedTime(out, field, observed or fallbackMs)


def optionTickStampTdxFieldClocks(out, existing, observedMs) -> None:
    """TDX 每次喂数都算「重新观测」→ 把它负责的字段时钟推进到本次观测时刻。

    为什么必须这样做（LESSON-020，实测数据）：
      `lastTime/qtyTime/oiTime` 原先只在 **QMT 补丁**到达时写
      （`optionTickFillQtyFromIncoming`），而 `OPT_QTY patch n=10` 每轮只轮转
      10 张 / 全链 650 张 → **每合约约 260s 才轮一次**。
      于是 T 板按 `lastTime` 判「最新」时，510050 链灰行 40% → 72%：
      那些值其实是 TDX 每轮都在重新观测的，时钟却不跟。

    观测时刻用**合并墙钟**，不用 TDX 自己的 `time`：
      实测（`scripts/probe_tdx_time_semantics.py`）TDX 的 `time` 在载荷未变时
      **13/13 次都不动** —— 它是「通达信缓存里这份快照多旧」，不是「我们何时观测到它」。
      而 daqmt 把 TDX 喂的数据合并进内存的那一拍墙钟，就是真正的观测时刻。

    只推进**当前值确实来自 TDX** 的字段：
      - book（五档）恒由 TDX 供 → 每次观测都推进；
      - last 取 `out` 里已落定的那个值（QMT 纠偏值可能覆盖过 TDX）→ 用字段自身
        时钟判断当前值是谁供的，是 TDX 才推进；
      - vol/oi 是 QMT 的槽位（`optionTickMergeKeepQty` 会保住 QMT 值）
        → 绝不跟着 TDX 推进，否则等于用 TDX 的观测时刻给 QMT 的值背书。

    不倒退：观测时刻更旧时原样保留（与 LESSON-018 同一条约束）。
    """
    if not isinstance(out, dict) or not isinstance(existing, dict):
        return
    obsMs = _optionTickTimeMsRaw(observedMs)
    if obsMs <= 0:
        return
    last = _asFloat(out.get("last"))
    if last is None:
        last = _asFloat(out.get("lastPrice"))
    tdxOwned = {
        "book": tickHasFiveBand(out)
        and str(out.get("bookSource") or "").strip().lower() != "qmt",
        "last": last is not None
        and last > 0
        and str(out.get("lastSource") or "").strip().lower() != "qmt",
    }
    for field in ("book", "last"):
        if not tdxOwned[field]:
            continue
        curMs = optionTickFieldTimeMs(existing, field)
        # 不倒退：已有更晚的观测（例如 QMT 补丁刚写过）就不用这次 TDX 的拍点顶掉
        if curMs > 0 and curMs > obsMs:
            continue
        optionTickSetFieldTime(out, field, obsMs)
    for field, valueKey in (("qty", "vol"), ("oi", "oi")):
        sourceKey = "qtySource" if field == "qty" else "oiSource"
        if str(out.get(sourceKey) or "").strip().lower() == "qmt":
            continue
        # 仅当该字段在本次 TDX 合并中真的被 TDX 写进去了，才推进它的时钟
        sourceChanged = (
            str(out.get(sourceKey) or "").strip().lower()
            != str(existing.get(sourceKey) or "").strip().lower()
        )
        if (
            _asFloat(out.get(valueKey)) == _asFloat(existing.get(valueKey))
            and not sourceChanged
        ):
            continue
        curMs = optionTickFieldTimeMs(existing, field)
        if curMs > 0 and curMs > obsMs:
            continue
        optionTickSetFieldTime(out, field, obsMs)


def optionTickQuoteFingerprint(tick) -> tuple:
    """不含时间：判断最新/买一卖一/量仓是否变化。"""
    rec = tick if isinstance(tick, dict) else {}
    last = _asFloat(rec.get("lastPrice"))
    if last is None:
        last = _asFloat(rec.get("last"))
    bid1 = _asFloat(rec.get("bid1") or rec.get("bidPrice1"))
    ask1 = _asFloat(rec.get("ask1") or rec.get("askPrice1"))
    bid = rec.get("bidPrice")
    ask = rec.get("askPrice")
    if bid1 is None and isinstance(bid, (list, tuple)) and bid:
        bid1 = _asFloat(bid[0])
    if bid1 is None:
        bid1 = _asFloat(bid)
    if ask1 is None and isinstance(ask, (list, tuple)) and ask:
        ask1 = _asFloat(ask[0])
    if ask1 is None:
        ask1 = _asFloat(ask)
    vol = _asFloat(rec.get("volume"))
    if vol is None:
        vol = _asFloat(rec.get("vol"))
    oi = _asFloat(rec.get("oi"))
    if oi is None:
        oi = _asFloat(rec.get("openInt"))
    if oi is None:
        oi = _asFloat(rec.get("openInterest"))
    return (last, bid1, ask1, vol, oi)


def optionTickBookFingerprint(tick) -> tuple:
    """完整五档价量指纹；盘口变动时不遗漏二至五档。"""
    rec = tick if isinstance(tick, dict) else {}

    def bookSide(key):
        values = rec.get(key)
        if not isinstance(values, (list, tuple)):
            return ()
        return tuple(_asFloat(value) for value in values[:5])

    return (
        bookSide("bidPrice"),
        bookSide("askPrice"),
        bookSide("bidVol"),
        bookSide("askVol"),
    )


def optionTickMergeKeepQty(existing, incoming, *, qmtFreshBeforeMs=None) -> dict:
    """写入前：缺 vol/oi 保留已有；已 QMT 补仓则量仓/最新不被滞后 TDX 回盖。

    保留某字段的旧值时，必须连它的专属时钟一起保留（`_carryFieldTime`），
    否则新 TDX tick 的时间会嫁接到旧量/旧仓上，造成字段级伪新鲜。
    """
    out = dict(incoming) if isinstance(incoming, dict) else {}
    if not isinstance(existing, dict):
        return out

    def _carry(field: str, *valueKeys) -> None:
        """搬运旧值及两类时钟，避免 TDX 新墙钟嫁接到 QMT 量仓。"""
        keys = OPT_TICK_FIELD_TIME_KEYS.get(field) or ()
        src = 0.0
        for k in keys:
            src = _optionTickTimeMsRaw(existing.get(k))
            if src > 0:
                break
        if src <= 0:
            src = optionTickTimeMs(existing)
        for k in valueKeys:
            if k not in out and existing.get(k) is not None:
                out[k] = existing.get(k)
        for k in keys:
            out[k] = src if src > 0 else 0
        optionTickCarryChangedTime(out, existing, field)

    inVol = _asFloat(out.get("vol"))
    if inVol is None:
        inVol = _asFloat(out.get("volume"))
    oldVol = _asFloat(existing.get("vol"))
    if oldVol is None:
        oldVol = _asFloat(existing.get("volume"))
    qtyFromQmt = bool(existing.get("qtyFromQmt"))

    def qmtFieldFresh(field: str) -> bool:
        """QMT 字段在 TDX 合并拍点仍可主导；无来源的旧形状保守保留。"""
        if not qtyFromQmt:
            return False
        sourceKey = {"last": "lastSource", "qty": "qtySource", "oi": "oiSource"}[field]
        source = str(existing.get(sourceKey) or "").strip().lower()
        # 旧快照还没有逐字段 source 时不能贸然换源；新形状必须按自身时钟判断。
        if not source:
            return True
        if source != "qmt":
            return False
        if qmtFreshBeforeMs is None:
            return True
        return optionTickFieldTimeMs(existing, field) >= float(qmtFreshBeforeMs)

    if qtyFromQmt:
        out["qtyFromQmt"] = True
        # QMT 字段在时效内才压住 TDX。回调沉默后，TDX 已重新观测到的
        # last/volume 应接管；OI 没有可信 TDX 来源，因此无论时效均保留 QMT 值。
        if oldVol is not None and (qmtFieldFresh("qty") or inVol is None):
            out["vol"] = oldVol
            out["volume"] = oldVol
            _carry("qty", "vol", "volume")
            if existing.get("qtySource"):
                out["qtySource"] = existing.get("qtySource")
        oldLast = _asFloat(existing.get("last"))
        if oldLast is None:
            oldLast = _asFloat(existing.get("lastPrice"))
        inLast = _asFloat(out.get("last"))
        if inLast is None:
            inLast = _asFloat(out.get("lastPrice"))
        if oldLast is not None and oldLast > 0 and (qmtFieldFresh("last") or inLast is None or inLast <= 0):
            out["last"] = oldLast
            out["lastPrice"] = oldLast
            _carry("last", "last", "lastPrice")
            if existing.get("lastSource"):
                out["lastSource"] = existing.get("lastSource")
        oldOi = _asFloat(existing.get("oi"))
        if oldOi is None:
            oldOi = _asFloat(existing.get("openInt"))
        if oldOi is None:
            oldOi = _asFloat(existing.get("openInterest"))
        if oldOi is not None:
            out["oi"] = oldOi
            out["openInt"] = oldOi
            _carry("oi", "oi", "openInt", "openInterest")
            if existing.get("oiSource"):
                out["oiSource"] = existing.get("oiSource")
    elif inVol is None and oldVol is not None:
        out["vol"] = oldVol
        out["volume"] = oldVol
        _carry("qty", "vol", "volume")
    elif (
        inVol is not None
        and oldVol is not None
        and oldVol > inVol + 1e-9
    ):
        out["vol"] = oldVol
        out["volume"] = oldVol
        _carry("qty", "vol", "volume")
    inOi = _asFloat(out.get("oi"))
    if inOi is None:
        inOi = _asFloat(out.get("openInt"))
    if inOi is None:
        inOi = _asFloat(out.get("openInterest"))
    if inOi is None and (not qtyFromQmt):
        oldOi = existing.get("oi")
        if oldOi is None:
            oldOi = existing.get("openInt")
        if oldOi is None:
            oldOi = existing.get("openInterest")
        if oldOi is not None:
            out["oi"] = oldOi
            out["openInt"] = oldOi
            _carry("oi", "oi", "openInt", "openInterest")
    return out


def isStaleIncomingTick(tickMs, existing, maxAgeSec: float = OPT_QMT_TICK_STALE_SEC) -> bool:
    """进来的 QMT tick 是否陈旧（冻结旧值/回盖），不得用它的 last/vol 回盖。

    判据（任一命中即视为陈旧）：
      1) 时间戳不可解析（0）→ 无法证明新鲜，保守视为陈旧；
      2) **早于**记录自身盘口时间超过 maxAgeSec → 是回盖；
      3) 时间戳不在今天 → 跨日残留。

    **方向性很重要**（2026-09-11 修）：早期实现用 `abs(refMs - ms)`，
    把「tick 比记录**新**」也判成陈旧 —— 现场 `existing=13:22:34`、
    `tick=13:28:48`（新 373s）被判陈旧 → 更新被丢弃 → 字段时钟永不推进，
    表现为 12 张活跃合约（成交量 500~4600）长期显示为陈旧。
    回盖只可能发生在 tick **更旧**时，故只判 `refMs - ms > maxAgeSec`。

    现场依据（2026-09-11）：`get_option_ticks` 整链卡在同一时刻数分钟，
    照抄其 vol/last 会把 TDX 的新鲜值回盖（TDX vol=3222 → QMT vol=3）。
    """
    if not isinstance(existing, dict):
        return True
    try:
        ms = float(tickMs)
    except (TypeError, ValueError):
        return True
    if ms <= 0:
        return True
    refMs = optionTickTimeMs(existing)
    if refMs > 0 and (refMs - ms) > float(maxAgeSec) * 1000.0:
        return True
    try:
        tickDay = datetime.fromtimestamp(ms / 1000.0).date()
    except (OSError, OverflowError, ValueError):
        return True
    return tickDay != date.today()


def optionTickFillQtyFromIncoming(existing, incoming) -> dict | None:
    """TDX 盘口锁住时：用 QMT 覆盖 vol/oi/last（交易所量仓源），不换买卖档。

    last 与量仓解耦：量仓未变时仍可单独刷新最新价。

    时钟语义（2026-09-11 修）：**不得用 QMT 时间覆盖 common `time`/`t`**，
    否则 TDX 盘口的真实时间被抹掉，昨日旧盘口会被下游判成「实时」（fail-open 缺陷）。
    common time 保持 TDX 盘口时钟；QMT 侧的成交/量/仓时间分别写入
    lastTime/qtyTime/oiTime，供 opt_t_board 做字段级 as-of 校验。

    陈旧保护（2026-09-11 修）：QMT `get_option_ticks` 会返回**冻结的旧 tick**
    （实测整链卡在同一时刻数分钟）。此时它的时间戳既不等于本地墙钟日，
    也可能早于记录自身盘口时间；若照抄它的 vol/oi/last 就会把 TDX 的
    新鲜成交量/最新价**回盖成旧值**（现场：TDX vol=3222 被盖成 QMT vol=3）。
    因此：陈旧 tick 的 last/vol 不采信，只允许它补 oi（持仓不必逐笔新鲜）。

    新鲜观测刷新时钟（2026-09-11 修，重要）：源端**新鲜**地报出该字段的当前值，
    即使值与本地相同，也要把该字段时钟推进到观测时刻。否则「值长期不变」的
    活跃合约（实测 510500 有 11 张成交量 500~34000 的合约）会被判为陈旧——
    因为旧实现只在「值变化」时才写补丁，等于从不确认「这个值现在仍然有效」。
    """
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return None
    if not existing.get("fromTdx"):
        return None
    changed = False
    out = dict(existing)
    # 该 tick 的时间戳：字段专属键优先，其次 common time/t
    tickMs = optionTickTimeMs(incoming)
    qmtStale = isStaleIncomingTick(tickMs, existing)
    exVol = _asFloat(existing.get("vol"))
    if exVol is None:
        exVol = _asFloat(existing.get("volume"))
    inVol = _asFloat(incoming.get("vol"))
    if inVol is None:
        inVol = _asFloat(incoming.get("volume"))
    if (not qmtStale) and inVol is not None and inVol >= 0:
        out["qtySource"] = "qmt"
        if exVol is None or abs(inVol - exVol) > 1e-9:
            out["vol"] = inVol
            out["volume"] = inVol
            out["volSource"] = "qmt"
            out["qtySource"] = "qmt"
            changed = True
        optionTickSetFieldTime(out, "qty", tickMs)
    exOi = _asFloat(existing.get("oi"))
    if exOi is None:
        exOi = _asFloat(existing.get("openInt"))
    inOi = _asFloat(incoming.get("oi"))
    if inOi is None:
        inOi = _asFloat(incoming.get("openInt"))
    if inOi is None:
        inOi = _asFloat(incoming.get("openInterest"))
    # 持仓量允许陈旧 tick 补（它不需要逐笔新鲜），但时间戳仍如实记录
    staleOiValueChanged = False
    if inOi is not None and inOi >= 0:
        if not qmtStale:
            out["oiSource"] = "qmt"
        if exOi is None or abs(inOi - exOi) > 1e-9:
            out["oi"] = inOi
            out["openInt"] = inOi
            out["oiSource"] = "qmt"
            staleOiValueChanged = bool(qmtStale)
            changed = True
        if not qmtStale:
            optionTickSetFieldTime(out, "oi", tickMs)
    inLast = _asFloat(incoming.get("last"))
    if inLast is None:
        inLast = _asFloat(incoming.get("lastPrice"))
    exLast = _asFloat(existing.get("last"))
    if exLast is None:
        exLast = _asFloat(existing.get("lastPrice"))
    if (not qmtStale) and inLast is not None and inLast > 0:
        out["lastSource"] = "qmt"
        if exLast is None or abs(inLast - exLast) > 1e-9:
            out["last"] = inLast
            out["lastPrice"] = inLast
            out["lastSource"] = "qmt"
            changed = True
        optionTickSetFieldTime(out, "last", tickMs)
    # QMT 量仓/成交与 TDX 盘口同表共存时，变动时钟仍按字段各自值维护；
    # 值未变只会刷新观测时钟，不会伪造“刚变动”。
    optionTickStampChangedFieldClocks(out, existing, tickMs)
    if staleOiValueChanged and tickMs > optionTickFieldChangedTimeMs(existing, "oi"):
        # OI 允许陈旧样本补值：它不是“本次重新观测”，所以 oiTime 不推进；
        # 但若数值确实变化，最后变动应如实标为 QMT 样本自身的事件时刻，
        # 不能沿用更早的 oiTime，也不能倒退已有更晚的变动时钟。
        optionTickSetFieldChangedTime(out, "oi", tickMs)
    if changed:
        # 盘口时间：TDX 原本的时间就是 common time/t，原样保留；若已另有专属键则沿用。
        if "bookTime" not in out and "book_time" not in out:
            bookMs = optionTickTimeMs(existing)
            if bookMs > 0:
                optionTickSetFieldTime(out, "book", bookMs)
        if not qmtStale:
            out["qtyFromQmt"] = True
        out["fromTdx"] = True
    if not changed:
        # 值没变但时钟推进了 → 仍需落库，否则新鲜度永远冻结。
        clocksAdvanced = any(
            out.get(k) != existing.get(k)
            for k in ("lastTime", "qtyTime", "oiTime")
        )
        if not clocksAdvanced:
            return None
        out["fromTdx"] = True
    return out


def optionTickPayloadChanged(existing, incoming) -> bool:
    """行情内容是否变化（忽略仅时间戳刷新）。"""
    if not isinstance(incoming, dict):
        return False
    if not isinstance(existing, dict):
        return True
    return (
        optionTickQuoteFingerprint(existing) != optionTickQuoteFingerprint(incoming)
        or optionTickBookFingerprint(existing) != optionTickBookFingerprint(incoming)
    )


def optionTickAgeSec(tick, now=None):
    """tick.time 相对墙钟的秒数；无时间戳返回 None。"""
    ms = optionTickTimeMs(tick)
    if ms <= 0:
        return None
    wall = now if isinstance(now, datetime) else datetime.now()
    return wall.timestamp() - (ms / 1000.0)


def optionTickRefreshReady(code, nowMono, lastMonoMap, cooldownSec) -> bool:
    last = float((lastMonoMap or {}).get(str(code).strip()) or 0.0)
    return float(nowMono) >= last + max(0.0, float(cooldownSec or 0.0))


def optionLiveRefreshQueue(codes, rotate: int, chunkSize: int) -> list:
    """轮转过期名单，避免只刷 ts_code 头部标的。"""
    items = [c for c in (codes or []) if str(c).strip()]
    if not items:
        return []
    n = len(items)
    size = max(1, int(chunkSize or 1))
    start = (int(rotate or 0) * size) % n
    return items[start:] + items[:start]


def optionLiveRefreshChunkSize(
    uniqueN,
    silenceSec,
    uniqueMin: int = OPT_LIVE_REFRESH_FAST_UNIQUE_MIN,
    silenceMax: float = OPT_LIVE_REFRESH_FAST_SILENCE_MAX,
    small: int = OPT_LIVE_REFRESH_CHUNK,
    large: int = OPT_LIVE_REFRESH_CHUNK_FAST,
) -> int:
    """全推站住用大片，否则退回 20，避免再堵 7214。"""
    if int(uniqueN or 0) >= int(uniqueMin) and float(silenceSec or 0.0) < float(silenceMax):
        return int(large)
    return int(small)


def optionLiveRefreshPriorityN(
    take, ratio: float = OPT_LIVE_REFRESH_PRIORITY_RATIO,
) -> int:
    n = max(1, int(take or 1))
    return min(n, max(0, int(n * float(ratio or 0.0))))


def optionLiveRefreshPriorityChunk(
    stale,
    rotate: int,
    take: int,
    tickMap,
    metaMap=None,
    dailyMap=None,
    priorityN=None,
) -> list:
    """前段按标的交错的有量优先，尾段轮转扫无量/远月。"""
    items = [c for c in (stale or []) if str(c).strip()]
    if not items:
        return []
    takeN = max(1, int(take or 1))
    if priorityN is None:
        priN = optionLiveRefreshPriorityN(takeN)
    else:
        priN = min(takeN, max(0, int(priorityN)))
    weights = {
        c: optionPollWeight(c, tickMap or {}, dailyMap or {}) for c in items
    }
    groups = groupOptionCodesByUnderlying(items, metaMap)
    for group in groups:
        group.sort(key=lambda c: -weights.get(c, 0.0))
    priority = []
    seen = set()
    for code in interleaveLists(groups):
        if len(priority) >= priN:
            break
        priority.append(code)
        seen.add(code)
    tailN = takeN - len(priority)
    if tailN <= 0:
        return priority
    tail = []
    for code in optionLiveRefreshQueue(items, rotate, max(1, tailN)):
        if code in seen:
            continue
        tail.append(code)
        if len(tail) >= tailN:
            break
    return priority + tail


def optionSzoLiveDownloadDue(
    nowMono, lastMono, minIntervalSec: float = OPT_SZO_LIVE_DL_MIN_INTERVAL_SEC,
) -> bool:
    last = float(lastMono or 0.0)
    if last <= 0.0:
        return True
    return float(nowMono or 0.0) >= last + max(0.0, float(minIntervalSec or 0.0))


def optionSzoLivePhaseSeconds(thirdPhaseEnable: bool | None = None) -> tuple[int, ...]:
    """返回 SZO 1m 自然分钟相位；第三相位仅供有界盘中 A/B。"""
    enabled = (
        OPT_SZO_MIN_THIRD_PHASE_ENABLE
        if thirdPhaseEnable is None
        else bool(thirdPhaseEnable)
    )
    return (8, 23, 38) if enabled else (8, 38)


def optionSzoLivePhaseKey(
    nowWall, *, thirdPhaseEnable: bool | None = None,
) -> str | None:
    """返回当前自然分钟已开放的 1m 下载相位。"""
    if not isinstance(nowWall, datetime):
        return None
    second = int(nowWall.second)
    phases = optionSzoLivePhaseSeconds(thirdPhaseEnable)
    phase = next((item for item in reversed(phases) if second >= item), None)
    if phase is None:
        return None
    return f"{nowWall:%Y%m%d%H%M}:{phase:02d}"


def optionSzoLivePhaseDue(
    nowWall, lastPhase, *, thirdPhaseEnable: bool | None = None,
) -> bool:
    phase = optionSzoLivePhaseKey(nowWall, thirdPhaseEnable=thirdPhaseEnable)
    return bool(phase and phase != str(lastPhase or ""))


def optionSzoLivePhaseWaitSec(
    nowWall, *, thirdPhaseEnable: bool | None = None,
) -> float | None:
    """返回距离下一次允许的 SZO 1m 下载相位的秒数。"""
    if not isinstance(nowWall, datetime):
        return None
    second = int(nowWall.second)
    phases = optionSzoLivePhaseSeconds(thirdPhaseEnable)
    nextPhase = next((item for item in phases if second < item), None)
    if nextPhase is not None:
        return float(nextPhase - second)
    return float(60 + phases[0] - second)


def optionSzoLiveShouldDownload(
    need, tickMap, asof, nowMono, lastRoundMono,
    minIntervalSec: float = OPT_SZO_LIVE_DL_MIN_INTERVAL_SEC,
    *,
    nowWall=None,
    lastPhase=None,
    phaseAlign: bool = False,
    thirdPhaseEnable: bool | None = None,
) -> bool:
    """无盘口立刻灌；仅刷新 1m 时按间隔降频。"""
    items = [c for c in (need or []) if str(c).strip()]
    if not items:
        return False
    ticks = tickMap or {}
    for code in items:
        rec = ticks.get(code)
        if rec is None:
            rec = ticks.get(str(code).strip().upper())
        if not optionTickLooksUsable(rec, asof):
            return True
    if phaseAlign:
        return optionSzoLivePhaseDue(
            nowWall, lastPhase, thirdPhaseEnable=thirdPhaseEnable,
        )
    return optionSzoLiveDownloadDue(nowMono, lastRoundMono, minIntervalSec)


def optionCodesNeedingLiveTickRefresh(
    codes, tickMap, asof, now=None, maxAgeSec: float = OPT_LIVE_TICK_STALE_SEC,
) -> list:
    """沪市：无可用盘口，或墙钟年龄超过 maxAgeSec。"""
    now = now or datetime.now()
    out = []
    ticks = tickMap or {}
    for code in optionShoCodes(codes):
        rec = ticks.get(code)
        if rec is None:
            rec = ticks.get(str(code).strip().upper())
        if not optionTickLooksUsable(rec, asof):
            out.append(code)
            continue
        age = optionTickAgeSec(rec, now)
        if age is None or age < 0 or age > float(maxAgeSec):
            out.append(code)
    return out


def optionShoLivePollCodes(codes, tickMap, cached, asof, maxAgeSec: float) -> list:
    """直播窗轮询：缓存未命中且内存还没有可用盘口。过期盘口走灌库。"""
    miss = optionCacheMissCodes(codes, cached, maxAgeSec)
    ticks = tickMap or {}
    return [c for c in miss if not optionTickLooksUsable(ticks.get(c), asof)]


def optionSzoLiveDownloadCodes(
    codes, tickMap, asof, now=None, lastMonoMap=None, nowMono: float = 0.0,
) -> list:
    """创业板：冷缺口/盘口过期优先，再按独立 OI 时钟补长尾。

    TDX 每秒推进盘口时钟，但不提供 OI；若只看通用 tick 时钟，
    “盘口新鲜、OI 陈旧”的合约会永久绕过 1m 下载。
    """
    now = now or datetime.now()
    nowMs = now.timestamp() * 1000.0
    ticks = tickMap or {}
    ranked = []
    for index, code in enumerate(optionSzoCodes(codes)):
        rec = ticks.get(code)
        if rec is None:
            rec = ticks.get(str(code).strip().upper())
        isCold = not optionTickLooksUsable(rec, asof)
        isQuoteStale = optionSzoMinuteBarStale(rec, now, OPT_SZO_MIN_STALE_SEC)
        oiTime = optionTickFieldTimeMs(rec, "oi")
        oiAgeSec = (nowMs - oiTime) / 1000.0 if oiTime > 0 else None
        isOiStale = bool(
            OPT_SZO_OI_AGE_SCHED_ENABLE
            and (oiAgeSec is None or oiAgeSec >= OPT_SZO_OI_REFRESH_STALE_SEC)
        )
        if not isCold and not isQuoteStale and not isOiStale:
            continue
        cooldown = OPT_SZO_MIN_FAIL_BACKOFF_SEC if isCold else OPT_LIVE_REFRESH_COOLDOWN_SEC
        if not optionTickRefreshReady(code, nowMono, lastMonoMap, cooldown):
            continue
        if isCold:
            rank = (0, index)
        elif isOiStale:
            # 未观测过 OI 的先补，其余最旧 oiTime 优先。
            rank = (1, oiTime if oiTime > 0 else -1.0, index)
        else:
            rank = (2, index)
        ranked.append((rank, code))
    return [code for _rank, code in sorted(ranked, key=lambda item: item[0])]


def optionSyncAddedN(result) -> int:
    if not isinstance(result, dict):
        return 0
    return len(result.get("added") or [])


def optionHeavyRpcDeferred(addedN: int, ownedGrew: bool = True) -> bool:
    """本轮有加订就让路。heal 退了再订时 owned 不变，也要让路。"""
    return int(addedN or 0) > 0


def shouldSkipShoForSzoMinute(needN: int, blockN: int = 20) -> bool:
    """创业板 1m 缺口仍大时本轮不打沪市 tick。"""
    return int(needN or 0) >= int(blockN)


def shouldSkipLiveDailyBars(szoHealthy: bool, addedN: int, failN: int, cacheN: int = 0) -> bool:
    """订阅未满、本轮在加订、tick 片失败、或回调已在供数时，1d 只会再堵 ROUTER。"""
    if int(cacheN or 0) >= 50:
        return True
    if not szoHealthy:
        return True
    if optionHeavyRpcDeferred(addedN):
        return True
    return int(failN or 0) >= int(OPT_POLL_FAIL_ABORT)


def optionShoPollBudget(missCodes, chunkSize: int, maxChunks: int, rotate: int = 0) -> list:
    """每轮 tick 张数上限；rotate 在 miss 名单上轮转，避免只打 ts_code 头部。"""
    size = max(1, int(chunkSize or 1))
    cap = max(0, int(maxChunks or 0)) * size
    items = [c for c in (missCodes or []) if str(c).strip()]
    if cap <= 0 or not items:
        return []
    n = len(items)
    take = min(cap, n)
    start = int(rotate or 0) % n
    return [items[(start + i) % n] for i in range(take)]


def optionQuoteSubIsHealthy(wanted, owned, maxOwned: int = 500) -> bool:
    """配额内订满即健康。沪市想订 508 张而配额 500，要求全订满会永远判 unhealthy。"""
    wantSet = {str(c).strip().upper() for c in (wanted or []) if str(c).strip()}
    if not wantSet:
        return True
    ownSet = {str(c).strip().upper() for c in (owned or []) if str(c).strip()}
    target = min(len(wantSet), max(0, int(maxOwned or 0)))
    return len(ownSet & wantSet) >= target


def shouldUseSzoDailyDownload(subscribeHealthy: bool, subscribeUnavailable: bool = False) -> bool:
    """仅当订阅 RPC 不可用时才灌创业板 1d；订阅进行中不要灌库抢 GIL。"""
    if subscribeHealthy:
        return False
    return bool(subscribeUnavailable)


def szoSubscribeInProgress(wanted, healthy: bool, unavailable: bool) -> bool:
    """创业板 1d 订阅尚未订满，且 RPC 可用。"""
    if healthy or unavailable:
        return False
    return bool(optionSzoCodes(wanted))


def optionDailyPollCodes(
    codes,
    szoHealthy: bool,
    szoUnavailable: bool,
    owned=None,
    listedToday=None,
) -> list:
    """订未满时主刷沪市；已订/新挂的 SZO 仍读 1d，避免创业板饿 6 分钟。"""
    items = list(codes or [])
    if not szoSubscribeInProgress(items, szoHealthy, szoUnavailable):
        return items
    allow = {str(c).strip() for c in list(owned or []) + list(listedToday or []) if str(c).strip()}
    extra = [c for c in optionSzoCodes(items) if c in allow]
    return optionShoCodes(items) + extra


def optionStubDailyDownloadCodes(codes, szoHealthy: bool, szoUnavailable: bool) -> list:
    """订未满只灌沪市占位柱；订满后含 SZO。"""
    items = list(codes or [])
    if szoSubscribeInProgress(items, szoHealthy, szoUnavailable):
        return optionShoCodes(items)
    return items


def optionDailyGateMode(wanted, szoHealthy: bool, szoUnavailable: bool) -> str:
    """sho_only=订未满只读沪市；all=订满读回含 SZO；szo_dl_fallback=订阅挂掉才灌库。"""
    if shouldUseSzoDailyDownload(szoHealthy, szoUnavailable):
        return "szo_dl_fallback"
    if szoSubscribeInProgress(wanted, szoHealthy, szoUnavailable):
        return "sho_only"
    return "all"


def applySzoSubSyncLocal(owned, result) -> set:
    """用本轮 sync 的 kept/added/dropped 对齐本地已订集合。"""
    base = {str(c).strip() for c in (owned or []) if str(c).strip()}
    if not isinstance(result, dict):
        return base
    kept = {str(c).strip() for c in (result.get("kept") or []) if str(c).strip()}
    added = {str(c).strip() for c in (result.get("added") or []) if str(c).strip()}
    drop = {str(c).strip() for c in (result.get("dropped") or []) if str(c).strip()}
    return (base - drop) | added | kept


def isSzoSubRpcUnavailable(exc) -> bool:
    text = str(exc or "").lower()
    return (
        "not allowed" in text
        or "not configured" in text
        or "not implemented" in text
    )


def shouldRefreshSzoDailyStore(nowMono: float, lastMono: float, intervalSec: float) -> bool:
    """本地 SZO 日K 不跟盘时，按间隔再灌一次当日 1d。"""
    return (nowMono - lastMono) >= float(intervalSec)


def shouldRefreshListedDailyStore(nowMono: float, lastMono: float, intervalSec: float) -> bool:
    """今日新挂本地 1d 冻结时，按间隔再灌当日柱。"""
    return (nowMono - lastMono) >= float(intervalSec)


def optionDailyDownloadStartYmd(asof) -> str:
    """新挂 1d 灌库起点：只拉 asof 当日柱。"""
    day = asof if isinstance(asof, date) else date.today()
    return "%04d%02d%02d" % (day.year, day.month, day.day)


def optionLiveListedDailyDownloadCodes(listedToday) -> list:
    """今日新挂全部要定期灌 1d：有量占位柱也会冻，不能走 stub 过滤。沪深交错。"""
    picked = [str(c).strip() for c in (listedToday or []) if str(c).strip()]
    sho = [c for c in picked if c.upper().endswith(".SHO")]
    rest = [c for c in picked if not c.upper().endswith(".SHO")]
    return interleaveTwo(sho, rest)


def interleaveLists(groups) -> list:
    """多列 round-robin 交错，用完短列后继续扫长列。"""
    cols = []
    for group in groups or []:
        col = [str(c).strip() for c in (group or []) if str(c).strip()]
        if col:
            cols.append(col)
    if not cols:
        return []
    if len(cols) == 1:
        return list(cols[0])
    idx = [0] * len(cols)
    out = []
    while True:
        moved = False
        for i, col in enumerate(cols):
            if idx[i] < len(col):
                out.append(col[idx[i]])
                idx[i] += 1
                moved = True
        if not moved:
            break
    return out


def interleaveTwo(left, right) -> list:
    """两列交错，避免创业板新挂永远排在沪市 90 张之后。"""
    return interleaveLists([left, right])


def interleaveOptionCodesByUnderlying(codes, metaMap=None) -> list:
    """五条链 round-robin，避免 50ETF 占满订阅配额和 tick 片窗口。"""
    return interleaveLists(groupOptionCodesByUnderlying(codes, metaMap))


def optionSzoTickHasSessionPrint(tick, asof) -> bool:
    """创业板本地 tick 须是当日 09:15 之后；昨收 15:00 / 午夜柱不算跟盘。"""
    if not isinstance(tick, dict) or asof is None:
        return False
    raw = tick.get("time") or tick.get("stime") or tick.get("t")
    tickDay = decodeQuoteTickDate(raw)
    if tickDay != asof:
        return False
    ts = optionTickStampSec(raw)
    if ts is None:
        return False
    return datetime.fromtimestamp(ts).time() >= dtTime(9, 15)


def optionTickPollCodes(codes, extraSzo=None) -> list:
    """历史：盘中灌库/冻文件只拉沪市。直播窗请用 optionLivePollCodes。"""
    out = optionShoCodes(codes)
    seen = {str(c).strip().upper() for c in out}
    for code in extraSzo or []:
        text = str(code).strip()
        if not text or text.upper() in seen:
            continue
        seen.add(text.upper())
        out.append(text)
    return out


def optionLivePollCodes(codes) -> list:
    """直播窗五档 tick 轮询仍只拉沪市（深市订 tick 无回调）。

    量仓纠偏见 pollOptionQtyPatchRound（SHO+SZO 均走 get_option_ticks）。
    """
    return optionShoCodes(codes)


def szoLiveTickDownloadStartYmd(asof) -> str:
    """只灌当日。往前 7 日会把昨收文件再灌回来。"""
    day = asof if isinstance(asof, date) else date.today()
    return "%04d%02d%02d" % (day.year, day.month, day.day)


def qmtDatPath(code: str, period: str) -> str | None:
    """`90007420.SZO` + `1m` → `<datadir>/SZO/60/90007420.DAT`；周期或代码不合法返回 None。"""
    text = str(code or "").strip()
    symbol, _, market = text.partition(".")
    symbol = symbol.strip()
    market = market.strip().upper()
    periodDir = QMT_DAT_PERIOD_DIR.get(str(period or "").strip())
    if not symbol or not market or not periodDir:
        return None
    return os.path.join(QMT_DATADIR, market, periodDir, symbol + ".DAT")


def qmtDatPriceScale(code: str = "", market: str = "") -> float:
    """期权 .DAT 存 4 位小数，股票/ETF 仍是 3 位。"""
    mkt = str(market or "").strip().upper()
    if not mkt:
        mkt = str(code or "").strip().upper().rpartition(".")[2]
    if mkt in ("SHO", "SZO"):
        return QMT_DAT_OPT_PRICE_SCALE
    return QMT_DAT_PRICE_SCALE


def parseQmtDatBytes(data: bytes, priceScale=None) -> list:
    """定长 64 字节小端记录 → dict 列表。

    time 统一输出毫秒，与 QMT RPC 返回对齐，下游 parseYmdBarDate /
    optionTickStampSec 走已验证分支。尾部不足 64 字节的残片自动丢弃。
    日线记录里没有 suspendFlag，固定填 0（见 PLAN 已知限制）。
    """
    try:
        scale = float(QMT_DAT_PRICE_SCALE if priceScale is None else priceScale)
    except (TypeError, ValueError):
        scale = QMT_DAT_PRICE_SCALE
    if scale <= 0:
        scale = QMT_DAT_PRICE_SCALE
    total = len(data) // QMT_DAT_RECORD_SIZE
    out = []
    for i in range(total):
        u = QMT_DAT_RECORD.unpack_from(data, i * QMT_DAT_RECORD_SIZE)
        ts = u[2]
        if ts <= 0:
            continue
        out.append(
            {
                "time": ts * 1000,
                "open": u[3] / scale,
                "high": u[4] / scale,
                "low": u[5] / scale,
                "close": u[6] / scale,
                "volume": u[8],
                "amount": float(u[10] | (u[11] << 32)),
                "openInterest": u[12],
                "preClose": u[15] / scale,
                "suspendFlag": 0,
            }
        )
    return out


def readQmtDatFile(path: str, maxRecords: int = 0, priceScale=None) -> list:
    """自文件尾反向取 maxRecords 条；多日 1m 文件可达 2 MB，不整读。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    total = size // QMT_DAT_RECORD_SIZE
    if total <= 0:
        return []
    offset = 0
    if maxRecords > 0 and total > maxRecords:
        offset = (total - maxRecords) * QMT_DAT_RECORD_SIZE
    try:
        with open(path, "rb") as fh:
            if offset:
                fh.seek(offset)
            data = fh.read()
    except OSError:
        return []
    return parseQmtDatBytes(data, priceScale)


def readQmtLocalBars(codes, period: str, maxRecords: int = 0) -> dict:
    """批量本地读 → {code: [记录,...]}；读不到的 code 不出现在结果里，交给调用方回退 RPC。"""
    out = {}
    if not QMT_DAT_ENABLED:
        return out
    for code in codes or []:
        path = qmtDatPath(code, period)
        if not path:
            continue
        records = readQmtDatFile(path, maxRecords, qmtDatPriceScale(code))
        if records:
            out[code] = records
    return out


def optionFrameToRecords(frame) -> list:
    """QMT DataFrame / 记录列表 → dict 列表；numpy 标量拆成 Python。"""
    if frame is None:
        return []
    records = []
    if hasattr(frame, "reset_index") and hasattr(frame, "to_dict"):
        try:
            if len(frame) <= 0:
                return []
        except TypeError:
            return []
        records = frame.reset_index().to_dict(orient="records")
    elif isinstance(frame, (list, tuple)):
        records = list(frame)
    elif isinstance(frame, dict):
        records = [frame]
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        item = {}
        for key, val in rec.items():
            if hasattr(val, "item"):
                try:
                    val = val.item()
                except Exception:
                    pass
            item[str(key)] = val
        out.append(item)
    return out


def szoMinuteRecordsToTick(records, asof) -> dict | None:
    """当日 1m 合成 L1：最新价=末根 close，成交量=当日分钟量之和，仓=末根有效 oi。"""
    if asof is None:
        return None
    lastClose = None
    lastOi = None
    lastT = None
    lastOiT = None
    volSum = 0.0
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        rawT = rec.get("time") or rec.get("index") or rec.get("stime")
        barDay = parseYmdBarDate(rawT)
        if barDay is None:
            barDay = decodeQuoteTickDate(rawT)
        if barDay != asof:
            continue
        ts = optionTickStampSec(rawT)
        if ts is not None and datetime.fromtimestamp(ts).time() < dtTime(9, 15):
            continue
        close = _asFloat(rec.get("close"))
        if close is None:
            close = _asFloat(rec.get("lastPrice"))
        vol = _asFloat(rec.get("volume"))
        oi = _asFloat(rec.get("openInterest"))
        if oi is None:
            oi = _asFloat(rec.get("openInt"))
        if oi is not None and oi > 0:
            lastOi = oi
            lastOiT = rawT
        if (close is None or close <= 0) and (vol is None or vol <= 0):
            continue
        if vol is not None and vol > 0:
            volSum += vol
        if close is not None and close > 0:
            lastClose = close
            lastT = rawT
    if lastClose is None or lastClose <= 0 or volSum <= 0:
        return None
    tick = {"lastPrice": lastClose, "volume": volSum, "time": lastT}
    if lastOi is not None:
        tick["openInt"] = lastOi
        tick["openInterest"] = lastOi
        tick["oiTime"] = lastOiT
    return tick


def szoMinuteRecordsToOiCandidate(
    records, asof, observedAtMs: float | None = None,
) -> dict | None:
    """从当日 1m 柱独立提取最新有效 OI，不要求该合约当日已有成交。

    远月/深度实值期权经常出现 ``volume=0`` 但 ``openInterest>0``。报价合成仍由
    :func:`szoMinuteRecordsToTick` 保持正成交量门槛；本函数只生成 OI 字段候选，
    调用方只能通过 ``overlaySzoMinuteOiCandidate`` 覆盖持仓字段。
    """
    if asof is None:
        return None
    if not OPT_MINUTE_OI_ONLY_ENABLE:
        quoteTick = szoMinuteRecordsToTick(records, asof)
        if not isinstance(quoteTick, dict) or quoteTick.get("openInt") is None:
            return None
        oiTimeMs = szoMinuteOiEffectiveTimeMs(
            quoteTick.get("oiTime"), observedAtMs
        )
        if oiTimeMs <= 0:
            return None
        return {
            "openInt": quoteTick.get("openInt"),
            "openInterest": quoteTick.get("openInterest"),
            "oiTime": oiTimeMs,
        }
    lastOi = None
    lastOiT = None
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        rawT = rec.get("time") or rec.get("index") or rec.get("stime")
        barDay = parseYmdBarDate(rawT)
        if barDay is None:
            barDay = decodeQuoteTickDate(rawT)
        if barDay != asof:
            continue
        ts = optionTickStampSec(rawT)
        if ts is not None and datetime.fromtimestamp(ts).time() < dtTime(9, 15):
            continue
        oi = _asFloat(rec.get("openInterest"))
        if oi is None:
            oi = _asFloat(rec.get("openInt"))
        if oi is not None and oi > 0:
            oiTimeMs = szoMinuteOiEffectiveTimeMs(rawT, observedAtMs)
            if oiTimeMs <= 0:
                continue
            lastOi = oi
            lastOiT = oiTimeMs
    if lastOi is None or lastOiT is None:
        return None
    return {
        "openInt": lastOi,
        "openInterest": lastOi,
        "oiTime": lastOiT,
    }


def overlaySzoMinuteOiCandidate(existing, minuteTick) -> dict | None:
    """仅用严格更新的 SZO 1m OI 推进正式持仓字段，不触碰其它行情字段。

    分钟合成记录的公共 ``time`` 属于最新 close，不能代表 OI 的观测时刻；这里只认
    显式 ``oiTime``。候选时间必须严格晚于正式 OI，缺时间、同刻或更旧都 fail-closed。
    """
    if not isinstance(minuteTick, dict):
        return None
    if not any(key in minuteTick for key in OPT_TICK_FIELD_TIME_KEYS["oi"]):
        return None
    minuteOi = _asFloat(
        minuteTick.get("openInt")
        if minuteTick.get("openInt") is not None
        else minuteTick.get("openInterest")
    )
    existing = existing if isinstance(existing, dict) else {}
    minuteMs = optionTickFieldTimeMs(minuteTick, "oi")
    existingMs = optionTickFieldTimeMs(existing, "oi")
    if minuteOi is None or minuteOi <= 0 or minuteMs <= 0 or minuteMs <= existingMs:
        return None
    existingOi = _asFloat(
        existing.get("oi")
        if existing.get("oi") is not None
        else existing.get("openInt")
    )
    out = dict(existing)
    out["oi"] = minuteOi
    out["openInt"] = minuteOi
    out["openInterest"] = minuteOi
    optionTickSetFieldTime(out, "oi", minuteMs)
    if existingOi is None or abs(float(minuteOi) - float(existingOi)) > 1e-9:
        optionTickSetFieldChangedTime(out, "oi", minuteMs)
    out["oiSource"] = "qmt_1m"
    return out


def szoMinuteOiShadowStats(existingMap, minuteTickMap) -> dict:
    """只读比较本地 1m OI 与正式快照；不得修改任一输入。"""
    statsOut = {
        "observed": 0,
        "newer": 0,
        "mismatch": 0,
        "candidate": 0,
        "missing_existing": 0,
    }
    existingMap = existingMap or {}
    for code, minuteTick in (minuteTickMap or {}).items():
        if not isinstance(minuteTick, dict):
            continue
        minuteOi = _asFloat(
            minuteTick.get("openInt")
            if minuteTick.get("openInt") is not None
            else minuteTick.get("openInterest")
        )
        if minuteOi is None or minuteOi <= 0:
            continue
        statsOut["observed"] += 1
        existing = existingMap.get(code) or {}
        existingOi = _asFloat(
            existing.get("oi")
            if existing.get("oi") is not None
            else existing.get("openInt")
        )
        minuteMs = optionTickFieldTimeMs(minuteTick, "oi")
        existingMs = optionTickFieldTimeMs(existing, "oi")
        newer = minuteMs > 0 and minuteMs > existingMs
        if newer:
            statsOut["newer"] += 1
            statsOut["candidate"] += 1
        if existingOi is None or existingOi <= 0:
            statsOut["missing_existing"] += 1
        elif newer and abs(float(minuteOi) - float(existingOi)) > 1e-9:
            statsOut["mismatch"] += 1
    return statsOut


def optionListedDailyDownloadChunk(need, cursor, chunkSize: int) -> tuple:
    """轮转切片，避免只反复灌名单头部。"""
    items = [str(c).strip() for c in (need or []) if str(c).strip()]
    if not items:
        return [], 0
    size = max(1, int(chunkSize))
    n = len(items)
    start = int(cursor or 0) % n
    take = min(size, n)
    chunk = [items[(start + i) % n] for i in range(take)]
    return chunk, (start + take) % n


def optionListedLookbackStart(
    asof,
    tradeDates=None,
    tradeDays: int = OPT_LISTED_LOOKBACK_TRADE_DAYS,
    calendarFallbackDays: int = OPT_LISTED_LOOKBACK_CAL_FALLBACK_DAYS,
):
    """最近 N 个交易日起点（含 asof 当日若开市）。日历不足则退 calendarFallbackDays。"""
    day = asof if isinstance(asof, date) else date.today()
    n = max(1, int(tradeDays))
    opened = []
    for raw in tradeDates or []:
        if isinstance(raw, datetime):
            parsed = raw.date()
        elif isinstance(raw, date):
            parsed = raw
        else:
            parsed = parseQmtExpireDate(raw)
        if parsed is not None and parsed <= day:
            opened.append(parsed)
    uniq = sorted(set(opened))
    if len(uniq) >= n:
        return uniq[-n]
    return day - timedelta(days=max(0, int(calendarFallbackDays)))


def noteOptChunkRound(failChunks: int, holdSec: float | None = None) -> None:
    """本轮有片失败则进入压力窗，后续临时缩小 get_option_ticks 分片。"""
    global _optChunkPressureUntil
    if int(failChunks or 0) <= 0:
        return
    hold = OPT_CHUNK_PRESSURE_SEC if holdSec is None else float(holdSec)
    until = time.monotonic() + max(0.0, hold)
    if until > float(_optChunkPressureUntil or 0.0):
        _optChunkPressureUntil = until


def isOptChunkUnderPressure(nowMono: float | None = None) -> bool:
    now = time.monotonic() if nowMono is None else float(nowMono)
    return now < float(_optChunkPressureUntil or 0.0)


def optPollChunkParams(underPressure: bool | None = None) -> dict:
    """直播窗轮询参数；压力期片更小、张数更少、片间让出更长。"""
    if underPressure is None:
        underPressure = isOptChunkUnderPressure()
    if underPressure:
        return {
            "chunkSize": max(1, int(OPT_CHUNK_PRESSURE_SIZE)),
            "maxChunks": max(0, int(OPT_CHUNK_PRESSURE_MAX_CHUNKS)),
            "yieldSec": max(0.0, float(OPT_CHUNK_PRESSURE_YIELD_SEC)),
            "timeoutSec": max(0.5, float(OPT_CHUNK_PRESSURE_TIMEOUT_SEC)),
        }
    return {
        "chunkSize": max(1, int(OPT_CHUNK_SIZE)),
        "maxChunks": max(0, int(OPT_LIVE_TICK_MAX_CHUNKS)),
        "yieldSec": max(0.0, float(OPT_CHUNK_YIELD_SEC)),
        "timeoutSec": max(0.5, float(OPT_RPC_TIMEOUT_SEC)),
    }


def optionLivePollWaitSec(
    inWindow: bool,
    nowMono: float,
    lastOptDailyMono: float,
    liveSec: float = OPT_DAILY_LIVE_SEC,
    idleSec: float = OPT_DAILY_IDLE_SEC,
    downloadTicks: bool = False,
    stillNeedN: int = 0,
    dlN: int = 0,
    restSec: float = OPT_LIVE_ROUND_REST_SEC,
    catchup: bool = False,
    failChunks: int = 0,
    failBackoffSec: float = OPT_FAIL_BACKOFF_SEC,
) -> float:
    """直播窗 round 后只歇 rest，不再按 1d 间隔再睡最多 LIVE 秒。
    tick 片失败则拉长等待，避免超时后 0.4s 再打。"""
    if int(failChunks or 0) > 0:
        return max(0.0, float(failBackoffSec))
    if inWindow or catchup:
        return max(0.0, float(restSec))
    if downloadTicks and int(stillNeedN or 0) > 0:
        return 0.2 if int(dlN or 0) > 0 else 2.0
    return float(idleSec)


def shouldFillOptionMeta(now: datetime) -> bool:
    """后台线程：盘中禁止全量 get_option_detail_data；直播窗结束后立刻接上。
    直播窗缺口由轮询线程 optionMetaFillBatchSize 窄口补，避免和 1d 抢锁。"""
    t = now.time()
    return t < dtTime(9, 15) or t >= dtTime(15, 31)


def optionMetaFillBatchSize(now: datetime) -> int:
    """盘前/15:31 后 20；直播窗每轮最多 8 张，避免 META 拖死下一轮 1d。"""
    t = now.time()
    if t < dtTime(9, 15) or t >= dtTime(15, 31):
        return OPT_META_BATCH
    return OPT_META_LIVE_GAP_BATCH


def shouldFillOptionMetaInPoll(
    yieldQuote: bool = False,
    skipHeavy: bool = False,
    deferHeavy: bool = False,
    wholeQuoteOk: bool = True,
) -> bool:
    """轮询线程补要素：全推让路/订未满不打；加订 defer 且全推不健康时也不打。"""
    if yieldQuote or skipHeavy:
        return False
    if deferHeavy and not wholeQuoteOk:
        return False
    return True


def optionMetaPreferCodes(prefer, need, have=None) -> list:
    """拉链新增排在缺要素名单前；已有 meta 的跳过。"""
    haveSet = {str(c).strip() for c in (have or []) if str(c).strip()}
    seen = set()
    out = []
    for raw in list(prefer or []) + list(need or []):
        text = str(raw).strip()
        if not text or text in seen or text in haveSet:
            continue
        seen.add(text)
        out.append(text)
    return out


def optionMetaNeedCodes(
    chainCodes,
    haveMeta,
    listedToday,
    triedAt,
    nowMono: float,
    retrySec: float,
) -> list:
    """缺要素优先今日新挂；compact 失败过期后重试，禁止本进程永久拉黑。"""
    have = {str(c).strip() for c in (haveMeta or []) if str(c).strip()}
    listedRaw = [str(c).strip() for c in (listedToday or []) if str(c).strip()]
    chain = [str(c).strip() for c in (chainCodes or []) if str(c).strip()]
    listedSet = set(listedRaw)
    listed = [c for c in listedRaw if c not in have]
    rest = [c for c in chain if c not in have and c not in listedSet]
    waitSec = float(retrySec)
    stamps = triedAt or {}

    def canTry(code) -> bool:
        ts = stamps.get(code)
        if ts is None:
            return True
        return (float(nowMono) - float(ts)) >= waitSec

    return [c for c in listed + rest if canTry(c)]


def undlSixFromMeta(meta) -> str:
    undl = str((meta or {}).get("undl") or "").strip().upper()
    return undl.split(".", 1)[0] if undl else ""


def groupOptionCodesByUnderlying(codes, metaMap=None) -> list:
    """按五标的拆链；整链一次 RPC（科创约 188 张），不要混成 728 张大片。"""
    metaMap = metaMap or {}
    buckets = {u.split(".", 1)[0]: [] for u in OPT_UNDERLYING_QMT}
    rest = []
    seen = set()
    for code in codes or []:
        if code in seen:
            continue
        seen.add(code)
        six = undlSixFromMeta(metaMap.get(code))
        if six in buckets:
            buckets[six].append(code)
        else:
            rest.append(code)
    groups = [buckets[u.split(".", 1)[0]] for u in OPT_UNDERLYING_QMT]
    groups = [g for g in groups if g]
    if rest:
        groups.append(rest)
    return groups


def optionCodesForUndlSix(codes, metaMap, undlSixes) -> list:
    """按标的六位筛合约，顺序与 codes 一致。"""
    want = {str(u).split(".", 1)[0] for u in (undlSixes or ()) if str(u).strip()}
    if not want:
        return []
    metaMap = metaMap or {}
    out = []
    seen = set()
    for code in codes or []:
        if code in seen:
            continue
        six = undlSixFromMeta(metaMap.get(code))
        if six in want:
            seen.add(code)
            out.append(code)
    return out


def optionTickTimeMs(tick) -> float:
    if not isinstance(tick, dict):
        return 0.0
    raw = tick.get("time") or tick.get("t") or 0
    return _optionTickTimeMsRaw(raw)


def shouldKeepTickOverMinute(existing, minuteTick, asof) -> bool:
    """已有当日成交且时间不早于新 1m 柱，则不要用更旧分钟盖掉。

    创业板不用五档：买卖档不再挡住 1m。
    """
    if not optionSzoTickHasSessionPrint(existing, asof):
        return False
    return optionTickTimeMs(existing) >= optionTickTimeMs(minuteTick)


def optionSzoMinuteBarStale(rec, now, graceSec: float = 8.0) -> bool:
    """已有 1m 合成时：要等这根分钟走完再加 grace，才算过期。"""
    tickMs = optionTickTimeMs(rec)
    if tickMs <= 0:
        return True
    try:
        tickDt = datetime.fromtimestamp(tickMs / 1000.0)
    except (OSError, OverflowError, ValueError):
        return True
    barEnd = tickDt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return now >= barEnd + timedelta(seconds=max(0.0, float(graceSec or 0.0)))


def optionSzoMinuteNeedCodes(
    codes, tickMap, asof, now=None, staleSec: float | None = 8.0, metaMap=None,
    skipUntil=None, nowMono=None, spotByUndl=None,
) -> list:
    """无当日成交、或上一根 1m 已走完的创业板，才读本地 1m；159915 优先。
    staleSec=None 只收无成交（冷缺口），不把过期 1m 当成挡沪市的理由。
    staleSec 当作新分钟开始后的落盘等待秒数。
    创业板不用五档：有买卖档也照样刷新 1m。
    """
    now = now or datetime.now()
    need = []
    ticks = tickMap or {}
    for code in optionSzoCodes(codes):
        rec = ticks.get(code)
        if rec is None:
            rec = ticks.get(str(code).strip().upper())
        meta = (metaMap or {}).get(code)
        last = None
        if isinstance(rec, dict):
            last = rec.get("lastPrice") or rec.get("last")
        six = undlSixFromMeta(meta) or "159915"
        if optionPremiumLooksImplausible(last, meta, (spotByUndl or {}).get(six)):
            continue
        if optionTickIsTdxFresh(rec):
            continue
        if not optionSzoTickHasSessionPrint(rec, asof):
            need.append(code)
            continue
        if staleSec is None:
            continue
        if optionSzoMinuteBarStale(rec, now, staleSec):
            need.append(code)
    skipUntil = skipUntil or {}
    if skipUntil and nowMono is not None:
        need = [
            c for c in need
            if float(nowMono) >= float(skipUntil.get(c) or skipUntil.get(str(c).strip()) or 0.0)
        ]
    if not metaMap:
        return need
    front = [c for c in need if undlSixFromMeta(metaMap.get(c)) == "159915"]
    rest = [c for c in need if c not in set(front)]
    return front + rest


def optionSzoMinuteMarkFailed(
    tried, tickMap, asof, nowMono: float, backoffSec: float, skipUntil=None,
) -> dict:
    """1m 仍合不成的合约写入 backoff，避免每秒空转。"""
    out = dict(skipUntil or {})
    until = float(nowMono) + max(0.0, float(backoffSec))
    ticks = tickMap or {}
    for code in tried or []:
        rec = ticks.get(code)
        if rec is None:
            rec = ticks.get(str(code).strip().upper())
        if optionSzoTickHasSessionPrint(rec, asof):
            out.pop(code, None)
            continue
        out[str(code).strip()] = until
    return out


def rotateList(items, offset: int) -> list:
    seq = list(items or [])
    n = len(seq)
    if n <= 1:
        return seq
    k = int(offset) % n
    return seq[k:] + seq[:k]


def undlFromOptBasicOptCode(optCode: str) -> str:
    """opt_basic.opt_code 形如 OP510300.SH → 510300.SH。"""
    text = str(optCode or "").strip().upper()
    if text.startswith("OP"):
        text = text[2:]
    return text


def optionPollWeight(code, tickMap, dailyMap) -> float:
    """轮询权重：已有成交量优先，T 型近月活跃档先更新。"""
    packed = tickMap.get(code) or {}
    vol = _asFloat(packed.get("vol"))
    if vol is None:
        vol = _asFloat(packed.get("volume"))
    if vol is None:
        vol = _asFloat((dailyMap.get(code) or {}).get("volume"))
    return float(vol or 0.0)


def optionIsAdjustedForPoll(code, adjustedSet=None, metaMap=None) -> bool:
    """名称尾 A 或乘数≠10000：T 型不展示，轮询放后面；IV 仍用其行情。"""
    if code in (adjustedSet or set()):
        return True
    meta = (metaMap or {}).get(code) or {}
    unit = meta.get("unit")
    if unit is None:
        return False
    try:
        return abs(float(unit) - 10000.0) > 0.1
    except (TypeError, ValueError):
        return False


def orderOptionCodesForPoll(
    codes,
    tickMap=None,
    dailyMap=None,
    adjustedSet=None,
    metaMap=None,
    listedSet=None,
) -> list:
    """新挂在前，标准 10000 非 A 在中（组内按成交量），调整合约在后。"""
    tickMap = tickMap or {}
    dailyMap = dailyMap or {}
    listed = {str(c).strip() for c in (listedSet or []) if str(c).strip()}
    return sorted(
        list(codes or []),
        key=lambda c: (
            0 if c in listed else 1,
            1 if optionIsAdjustedForPoll(c, adjustedSet, metaMap) else 0,
            -optionPollWeight(c, tickMap, dailyMap),
        ),
    )


def _fillOptDailySideFields(packed, oi, pre) -> dict:
    """tick 主路径补 1d 的仓/昨收，不改 last/vol。"""
    if oi is not None and packed.get("oi") is None and packed.get("openInt") is None:
        packed["oi"] = oi
        packed["openInt"] = oi
    if pre is not None and packed.get("lastClose") is None:
        packed["lastClose"] = pre
    packed["fromDaily"] = False
    packed.pop("staleLast", None)
    return packed


def applyOptDailySession(
    packed, daily, asof, now=None, listedToday: bool = False, meta=None, spot=None,
) -> dict:
    """新鲜 tick 优先；同日 tick 量不少于 1d 也保留（SZO 午夜柱会把量盖小）。
    有买卖档、新挂 10 倍日线、或权利金不像价时，只补仓/昨收，不盖 last。"""
    if not isinstance(packed, dict) or not isinstance(daily, dict) or asof is None:
        return packed
    if daily.get("d") != asof:
        return packed
    close = _asFloat(daily.get("close"))
    vol = _asFloat(daily.get("volume"))
    oi = _asFloat(daily.get("oi"))
    pre = _asFloat(daily.get("preClose"))
    tickDay = decodeQuoteTickDate(packed.get("t"))
    tickLast = _asFloat(packed.get("last"))
    tickVol = _asFloat(packed.get("vol"))
    if tickVol is None:
        tickVol = _asFloat(packed.get("volume"))
    hasLiveTick = (
        tickLast is not None
        and tickLast > 0
        and tickVol is not None
        and tickVol > 0
        and (tickDay is None or tickDay == asof)
    )
    dailyReady = vol is not None and vol > 0 and close is not None and close > 0
    if optionTickIsFresh(packed, asof, now=now):
        return _fillOptDailySideFields(packed, oi, pre)
    tickAhead = hasLiveTick and (
        not dailyReady or vol is None or tickVol >= vol
    )
    if tickAhead:
        return _fillOptDailySideFields(packed, oi, pre)
    closeUse = close
    if optionPremiumLooksImplausible(close, meta, spot):
        scaled = scaleTenXOptLast(close, meta, spot)
        if scaled is None:
            return _fillOptDailySideFields(packed, oi, pre)
        closeUse = scaled
    if (
        tickHasFiveBand(packed)
        or not optionDailyLastAllowed(
            close, packed, listedToday=listedToday, meta=meta, spot=spot,
        )
        or optionPremiumLooksImplausible(closeUse, meta, spot)
    ):
        return _fillOptDailySideFields(packed, oi, pre)
    if dailyReady:
        packed["last"] = closeUse
        packed["lastPrice"] = closeUse
        packed["vol"] = vol
        packed["volume"] = vol
        if oi is not None:
            packed["oi"] = oi
            packed["openInt"] = oi
        if pre is not None:
            packed["lastClose"] = pre
        if daily.get("t") is not None:
            packed["t"] = daily.get("t")
        packed["fromDaily"] = True
        packed.pop("staleLast", None)
        return packed
    fallback = closeUse if closeUse is not None and closeUse > 0 else pre
    if (
        not optionDailyLastAllowed(
            close if close is not None else fallback,
            packed,
            listedToday=listedToday,
            meta=meta,
            spot=spot,
        )
        or optionPremiumLooksImplausible(fallback, meta, spot)
    ):
        return _fillOptDailySideFields(packed, oi, pre)
    packed["last"] = fallback if fallback is not None and fallback > 0 else (
        pre if pre is not None and pre > 0 else packed.get("last")
    )
    packed["lastPrice"] = packed.get("last")
    packed["vol"] = 0.0
    packed["volume"] = 0.0
    if oi is not None:
        packed["oi"] = oi
        packed["openInt"] = oi
    if pre is not None and pre > 0:
        packed["lastClose"] = pre
    packed["fromDaily"] = True
    return packed


def etfBarLooksStub(bar) -> bool:
    """今日无量或 OHLC 粘在同一价，视为未结算日K 占位。"""
    if not isinstance(bar, dict):
        return True
    close = _asFloat(bar.get("close"))
    if close is None or close <= 0:
        return True
    vol = _asFloat(bar.get("volume"))
    if vol is not None and vol <= 0:
        return True
    ohlc = [_asFloat(bar.get(k)) for k in ("open", "high", "low", "close")]
    if all(v is not None for v in ohlc) and ohlc[0] == ohlc[1] == ohlc[2] == ohlc[3]:
        return True
    return False


def etfDailyBarFromRtkRow(row, asof) -> dict | None:
    """stock_wind.rt_k 当日快照 → 与日K 相同的 bar 结构。"""
    if not isinstance(row, dict) or asof is None:
        return None
    tt = row.get("trade_time")
    barDay = tt.date() if hasattr(tt, "date") else parseYmdBarDate(tt)
    if barDay != asof:
        return None
    close = _asFloat(row.get("close"))
    if close is None or close <= 0:
        return None
    ts = None
    if hasattr(tt, "timestamp"):
        ts = tt.timestamp() * 1000.0
    return {
        "d": barDay,
        "close": close,
        "open": _asFloat(row.get("open")),
        "high": _asFloat(row.get("high")),
        "low": _asFloat(row.get("low")),
        "volume": _asFloat(row.get("vol")),
        "preClose": _asFloat(row.get("pre_close")),
        "t": ts,
    }


def applyEtfDailySession(packed, daily, asof) -> dict:
    """无当日 ETF tick 时用日K 填 last/OHLC。直播窗现货以全推为准，本函数只在缺 last 时兜底。"""
    if not isinstance(packed, dict) or not isinstance(daily, dict) or asof is None:
        return packed
    if daily.get("d") != asof:
        return packed
    tickDay = decodeQuoteTickDate(packed.get("t"))
    liveLast = _asFloat(packed.get("last"))
    if liveLast is not None and liveLast > 0 and (tickDay is None or tickDay == asof):
        return packed
    if etfBarLooksStub(daily):
        return packed
    close = _asFloat(daily.get("close"))
    if close is None or close <= 0:
        return packed
    packed["last"] = close
    packed["lastPrice"] = close
    for key in ("open", "high", "low"):
        val = _asFloat(daily.get(key))
        if val is not None:
            packed[key] = val
    vol = _asFloat(daily.get("volume"))
    if vol is not None:
        packed["vol"] = vol
        packed["volume"] = vol
    pre = _asFloat(daily.get("preClose"))
    if pre is not None:
        packed["lastClose"] = pre
    if daily.get("t") is not None:
        packed["t"] = daily.get("t")
    packed["fromDaily"] = True
    return packed


def buildQuoteSnapshot() -> dict:
    """五只 ETF + 期权链内存快照（slim 字段）；不含全市场 7230 只。"""
    with _cacheLock:
        asof = date.today()
        etfTickMap = {code: etfTicks.get(code) for code in OPT_UNDERLYING_QMT}
        etfDailyMap = dict(etfDaily)
        dailyMap = dict(optionDaily)
        tickMap = dict(optionTicks)
        listedTodaySet = set(_optionListedToday)
        etfLast = stats.get("lastPushWall")
        optLast = optionStats.get("lastWall")
        metaMap = {k: dict(v) for k, v in optionMeta.items() if v}
        spotByUndl = etfSpotByUndlSix(etfTickMap, etfDailyMap)
    etfOut = {}
    for code in OPT_UNDERLYING_QMT:
        packed = compactQuoteTick(etfTickMap.get(code), asofDate=asof) or {
            "last": None,
            "vol": None,
            "oi": None,
        }
        packed = applyEtfDailySession(packed, etfDailyMap.get(code), asof)
        if packed.get("last") is not None:
            etfOut[code] = packed
    optOut = {}
    for code in set(tickMap) | set(dailyMap):
        packed = compactQuoteTick(
            tickMap.get(code) or {},
            fillLastFromMid=False,
            asofDate=asof,
        )
        if not packed:
            packed = {"last": None, "vol": None, "oi": None}
        meta = metaMap.get(code)
        six = undlSixFromMeta(meta)
        if not six and str(code).upper().endswith(".SZO"):
            six = "159915"
        # 来源标注（2026-09-11）：510050 / 159915 的期权由通达信供给。
        # 保留在快照里（T 板靠本快照的 optMeta 找合约，摘掉会让整链退回日线），
        # 但显式标记来源，避免 T 板把 TDX 来的盘口误判成 QMT 实时。
        if six and six not in OPT_QMT_TICK_UNDERLYINGS:
            packed["src"] = "tdx"
        packed = applyOptDailySession(
            packed,
            dailyMap.get(code),
            asof,
            listedToday=code in listedTodaySet,
            meta=meta,
            spot=spotByUndl.get(six),
        )
        packed = dropImplausibleOptLast(packed, meta, spotByUndl.get(six))
        if packed.get("last") is None and packed.get("vol") is None and packed.get("oi") is None:
            continue
        optOut[code] = packed
    return {
        "ok": True,
        "etf": etfOut,
        "opt": optOut,
        "etfN": len(etfOut),
        "optN": len(optOut),
        "etfWall": etfLast.isoformat(timespec="seconds") if etfLast else None,
        "optWall": optLast.isoformat(timespec="seconds") if optLast else None,
        "optMeta": metaMap,
        "optMetaN": len(metaMap),
        "optOiMinute": shoMinuteOiHealthSnapshot(),
    }


def invalidateQuoteSnapshotJson() -> None:
    """tick/1d 写入后丢弃序列化缓存，避免 HTTP 读到过期快照。"""
    global _snapJsonCache
    with _snapJsonLock:
        _snapJsonCache = (0.0, b"")


def encodeQuoteSnapshotJson() -> bytes:
    """ThreadingHTTPServer 并发读快照：短 TTL 复用序列化结果，避免反复 json.dumps。"""
    global _snapJsonCache
    now = time.monotonic()
    with _snapJsonLock:
        t0, body = _snapJsonCache
        if body and (now - t0) < SNAP_JSON_TTL_SEC:
            return body
    raw = json.dumps(buildQuoteSnapshot(), ensure_ascii=False, default=str).encode("utf-8")
    with _snapJsonLock:
        _snapJsonCache = (time.monotonic(), raw)
    return raw


class QuoteSnapshotHandler(BaseHTTPRequestHandler):
    """仅 127.0.0.1 快照；不写库。"""

    def log_message(self, fmt, *args) -> None:
        return

    def do_GET(self) -> None:
        path = (self.path or "/").split("?", 1)[0]
        if path not in ("/snapshot", "/"):
            self.send_error(404)
            return
        body = encodeQuoteSnapshotJson()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def startQuoteHttp(logger):
    try:
        server = ThreadingHTTPServer((QUOTE_HTTP_HOST, QUOTE_HTTP_PORT), QuoteSnapshotHandler)
    except OSError as exc:
        logger.error("行情快照 HTTP 未启动 %s:%s %s", QUOTE_HTTP_HOST, QUOTE_HTTP_PORT, exc)
        return None
    thread = threading.Thread(target=server.serve_forever, name="daqmt-quote-http", daemon=True)
    thread.start()
    logger.info("行情快照 HTTP http://%s:%s/snapshot", QUOTE_HTTP_HOST, QUOTE_HTTP_PORT)
    return server


def qmtCodesFromOptBasicRows(tsCodes) -> list:
    codes = []
    seen = set()
    for raw in tsCodes or []:
        try:
            qmt = tsCodeToQmtOpt(raw)
        except ValueError:
            continue
        if qmt in seen:
            continue
        seen.add(qmt)
        codes.append(qmt)
    return codes


def optUnderlyingQmtCodes(underlyings=None) -> tuple:
    """六位代码补市场后缀；创业板 ETF 用 .SZ。"""
    names = tuple(underlyings or OPT_UNDERLYINGS)
    out = []
    for raw in names:
        text = str(raw or "").strip().upper()
        if not text:
            continue
        if text.endswith(".SH") or text.endswith(".SZ"):
            out.append(text)
        elif text == "159915":
            out.append("159915.SZ")
        else:
            out.append("%s.SH" % text)
    return tuple(out) if out else OPT_UNDERLYING_QMT


def extractOptionCodesFromUndlData(raw) -> list:
    rows = raw
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), (list, tuple)):
            rows = raw.get("data")
        else:
            rows = []
            for key, val in raw.items():
                if isinstance(val, (list, tuple)):
                    rows.extend(val)
                elif isinstance(val, str):
                    rows.append(val)
                else:
                    rows.append(key)
    if not isinstance(rows, (list, tuple)):
        return []
    picked = []
    for row in rows:
        if isinstance(row, str):
            picked.append(row)
        elif isinstance(row, dict):
            picked.append(
                row.get("option_code")
                or row.get("stock_code")
                or row.get("ts_code")
                or row.get("code")
                or ""
            )
    return qmtCodesFromOptBasicRows(picked)


def optionChainMeetsFloor(
    perCounts,
    minPer: int = OPT_CHAIN_MIN_PER,
    minTotal: int = OPT_CHAIN_MIN_TOTAL,
) -> bool:
    counts = dict(perCounts or {})
    if len(counts) < len(OPT_UNDERLYING_QMT):
        return False
    if any(int(n or 0) < minPer for n in counts.values()):
        return False
    return sum(int(n or 0) for n in counts.values()) >= minTotal


def shouldRefreshOptionChain(asof, lastAsof) -> bool:
    """日切或尚未拉链成功（lastAsof is None）时重拉 QMT 链。"""
    return lastAsof is None or asof != lastAsof


def optionChainZipGrew(newN, seedN) -> bool:
    return int(newN or 0) > int(seedN or 0)


def optionChainZipSameSizeSettled(
    now,
    sameSizeHits: int,
    settleTime=None,
    minHits: int = OPT_CHAIN_SETTLE_HITS,
) -> bool:
    """QMT 与 opt_basic 张数相同：09:35 后再确认 minHits 次视为已对齐。"""
    t = now.time() if isinstance(now, datetime) else now
    settle = settleTime if settleTime is not None else dtTime(9, 35)
    return t >= settle and int(sameSizeHits) >= int(minHits)


def decideOptionChainZipState(grew: bool, sameHits: int, now) -> str:
    """complete=当天可锁；retry=QMT 可能尚未挂出新月。"""
    if grew:
        return "complete"
    if optionChainZipSameSizeSettled(now, sameHits):
        return "complete"
    return "retry"


def shouldConfirmOptionChainZip(
    now,
    chainComplete: bool,
    lastZipMono: float,
    nowMono: float,
    confirmSec: float = OPT_CHAIN_CONFIRM_SEC,
    confirmUntil=None,
) -> bool:
    """已锁链后 11:30 前按间隔再确认，防止开盘后才挂齐。"""
    if not chainComplete:
        return False
    t = now.time() if isinstance(now, datetime) else now
    until = confirmUntil if confirmUntil is not None else dtTime(11, 30)
    if t >= until:
        return False
    return (float(nowMono) - float(lastZipMono)) >= float(confirmSec)


def shouldAttemptOptionChainZip(
    asof,
    lastAsof,
    nowMono: float,
    zipReadyMono: float,
    chainFailUntil: float,
    chainComplete: bool = False,
    lastZipMono: float = 0.0,
    retrySec: float | None = None,
    now=None,
    confirmSec: float | None = None,
) -> bool:
    """启动后等 zipReady 再拉链；未增长按 retrySec 重试；失败退避期内不打。
    已完成则 11:30 前仍可确认。日切立即拉。"""
    if nowMono < float(chainFailUntil):
        return False
    waitSec = (
        OPT_CHAIN_INCOMPLETE_RETRY_SEC if retrySec is None else float(retrySec)
    )
    confSec = OPT_CHAIN_CONFIRM_SEC if confirmSec is None else float(confirmSec)
    if lastAsof is not None and asof != lastAsof:
        return True
    if chainComplete:
        if now is None:
            return False
        return shouldConfirmOptionChainZip(
            now, True, lastZipMono, nowMono, confSec
        )
    if lastZipMono <= 0:
        return nowMono >= float(zipReadyMono)
    return (nowMono - float(lastZipMono)) >= waitSec


def optionChainDelta(oldCodes, newCodes):
    oldList = list(oldCodes or [])
    newList = list(newCodes or [])
    oldSet = set(oldList)
    newSet = set(newList)
    added = [c for c in newList if c not in oldSet]
    dropped = [c for c in oldList if c not in newSet]
    return added, dropped


def pruneOptionCaches(keepCodes) -> int:
    keep = set(keepCodes or [])
    n = 0
    with _cacheLock:
        for store in (optionTicks, optionDaily, optionMeta):
            dead = [c for c in list(store.keys()) if c not in keep]
            for code in dead:
                store.pop(code, None)
                n += 1
    return n


def parseQmtExpireDate(raw):
    text = str(raw or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def optionCallPutFromDetail(detail) -> str | None:
    """认购认沽：optType，或沪深期权 ProductType 0=购 1=沽。"""
    if not isinstance(detail, dict):
        return None
    for key in ("optType", "ProductType", "opt_type"):
        raw = detail.get(key)
        if raw is None or raw == "":
            continue
        text = str(raw).strip().upper()
        if text.endswith(".0") and len(text) > 2:
            text = text[:-2]
        if text in ("C", "CALL"):
            return "C"
        if text in ("P", "PUT"):
            return "P"
        try:
            n = int(float(text))
        except (TypeError, ValueError):
            continue
        if n == 0:
            return "C"
        if n == 1:
            return "P"
    return None


def compactOptionMeta(qmtCode, detail) -> dict | None:
    """T 型补行：行权价/到期/认购认沽/乘数/标的。A 类仍以 opt_basic 已有行排除。"""
    if not str(qmtCode or "").strip() or not isinstance(detail, dict):
        return None
    strike = _asFloat(detail.get("OptExercisePrice"))
    exp = parseQmtExpireDate(detail.get("ExpireDate") or detail.get("EndDelivDate"))
    cp = optionCallPutFromDetail(detail)
    unit = _asFloat(detail.get("OptUnit"))
    if unit is None:
        unit = _asFloat(detail.get("VolumeMultiple"))
    undl = str(detail.get("OptUndlCode") or "").strip()
    if strike is None or strike <= 0 or exp is None or cp is None or not undl:
        return None
    return {
        "k": strike,
        "exp": exp.isoformat(),
        "sm": "%04d%02d" % (exp.year, exp.month),
        "cp": cp,
        "unit": unit,
        "undl": undl,
    }


def compactOptionMetaFromBasicRow(tsCode, callPut, strike, maturity, perUnit, optCode):
    """opt_basic 行 → (qmtCode, meta)；T 型 undl 对齐六位代码。"""
    try:
        qmt = tsCodeToQmtOpt(tsCode)
    except ValueError:
        return None
    cp = str(callPut or "").strip().upper()
    if cp in ("CALL", "0"):
        cp = "C"
    elif cp in ("PUT", "1"):
        cp = "P"
    k = _asFloat(strike)
    unit = _asFloat(perUnit)
    if unit is None:
        unit = 10000.0
    exp = maturity if isinstance(maturity, date) else parseQmtExpireDate(maturity)
    undl = undlFromOptBasicOptCode(optCode)
    if k is None or k <= 0 or exp is None or cp not in ("C", "P") or not undl:
        return None
    return qmt, {
        "k": k,
        "exp": exp.isoformat(),
        "sm": "%04d%02d" % (exp.year, exp.month),
        "cp": cp,
        "unit": unit,
        "undl": undl,
    }


def _setOptionChainCodes(codes) -> None:
    global _optionChainCodes
    keep = [str(c).strip() for c in (codes or []) if str(c).strip()]
    keepSet = set(keep)
    with _cacheLock:
        _optionChainCodes = keep
        deadTried = [c for c in list(_optionMetaTriedAt) if c not in keepSet]
        for code in deadTried:
            _optionMetaTriedAt.pop(code, None)


def mergeOptionUniverseCodes(primary, fallback) -> list:
    """合并 QMT 主链与已准备好的 TDX 五链，保留主链顺序并去重。

    ``resolveEtfOptionCodes(skipPing=True)`` 为避免启动后再次争用桥接，
    只返回三条 QMT 主链；启动前的 ``prepareTdxFallbackChain`` 已经准备了
    510050/159915 的 TDX 代码和元数据。两者必须在进入轮询前合并，否则
    重启后会出现 ``szo=0``，159915 的盘口/OI 长尾直接从调度宇宙消失。

    该函数只做内存列表合并，不改变订阅配额：
    ``optionQuoteSubscribeCodes`` 后续仍只选择三条 QMT 主导链。
    """
    out = []
    seen = set()
    for raw in (*list(primary or []), *list(fallback or [])):
        text = str(raw).strip()
        if not text:
            continue
        key = text.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _copyOptionMeta() -> dict:
    with _cacheLock:
        return {k: dict(v) for k, v in optionMeta.items() if v}


def _setAdjustedOptionCodes(codes) -> None:
    global _adjustedOptionCodes
    with _cacheLock:
        _adjustedOptionCodes = set(codes or [])


def _copyAdjustedOptionCodes() -> set:
    with _cacheLock:
        return set(_adjustedOptionCodes)


def loadEtfOptionCodesFromQmt(
    xtdata,
    undlCodes=None,
    timeoutSec: float = OPT_CHAIN_RPC_TIMEOUT_SEC,
):
    """订全推之前拉链。禁止传空标的（空串=全市场）。"""
    undls = tuple(undlCodes or optUnderlyingQmtCodes())
    merged = []
    seen = set()
    per = {}
    call = xtdata.client.call
    for undl in undls:
        if not str(undl).strip():
            raise ValueError("undl_code_ref must not be empty")
        gotLock = _acquireBackgroundRpcLock()
        if not gotLock:
            break
        try:
            raw = call(
                "get_option_undl_data",
                {"undl_code_ref": undl},
                timeout_seconds=timeoutSec,
            )
        finally:
            _rpcLock.release()
        part = extractOptionCodesFromUndlData(raw)
        per[undl] = len(part)
        for code in part:
            if code in seen:
                continue
            seen.add(code)
            merged.append(code)
    return merged, per


def resolveEtfOptionCodes(logger, xtTrader, xtdata, skipPing: bool = False):
    """刚订完全推时禁止再打 QMT 拉链（get_option_undl_data 会抢 ROUTER）。
    其它入口仍可拉链；张数不够或异常则 opt_basic。轮询线程仅日切重拉。"""
    if skipPing:
        logger.info("订全推后跳过 QMT 拉链，改用 opt_basic")
        try:
            fb = loadEtfOptionQmtCodes()
            return fb, "opt_basic"
        except Exception as exc:
            logger.error("opt_basic 加载失败: %s", exc)
            return [], "none"
    pingOk = False
    try:
        pingMs = _rpcPingMs(xtTrader)
        pingOk = True
        logger.info("启动 ping_ms=%.1f，尝试 QMT 期权拉链", pingMs)
    except Exception as exc:
        logger.warning("启动 ping 失败，跳过 QMT 拉链: %s", exc)
    if pingOk:
        try:
            codes, per = loadEtfOptionCodesFromQmt(xtdata)
            logger.info(
                "QMT 期权链 %s total=%d",
                ",".join("%s=%d" % (k, v) for k, v in per.items()),
                len(codes),
            )
            if optionChainMeetsFloor(per):
                return codes, "qmt"
            logger.warning("QMT 期权链张数不够，回退 opt_basic")
        except Exception as exc:
            logger.warning("QMT 期权链异常，回退 opt_basic: %s", exc)
    try:
        fb = loadEtfOptionQmtCodes()
        return fb, "opt_basic"
    except Exception as exc:
        logger.error("opt_basic 加载失败: %s", exc)
        return [], "none"


def chunkCodes(codes, size) -> list:
    items = list(codes or [])
    step = max(1, int(size))
    return [items[i:i + step] for i in range(0, len(items), step)]


def isOptionPollWindow(now: datetime) -> bool:
    """期权直播窗：[09:15, 15:31)，含午休。窗内主刷当日 1d；无当日 tick 的缺口（新月）可小片灌 tick。
    ETF 现货走全推；窗内不打 ETF_DAILY（五只都无当日 last 才 30s 兜底）。"""
    t = now.time()
    return dtTime(9, 15) <= t < dtTime(15, 31)


def isOptionHeavyRpcWindow(now: datetime) -> bool:
    """允许期权重型 QMT RPC 的窗口；午休仅保留有界收尾。"""
    t = now.time()
    morningEnd = now.replace(hour=11, minute=30, second=0, microsecond=0)
    lunchCutoff = morningEnd + timedelta(seconds=OPT_LUNCH_HEAVY_GRACE_SEC)
    return (
        dtTime(9, 15) <= t < dtTime(11, 30)
        or morningEnd <= now < lunchCutoff
        or dtTime(13, 0) <= t < dtTime(15, 31)
    )


def isOptionPreopenBootstrapWindow(now: datetime) -> bool:
    """仅盘前允许日线、合约要素等启动基线；盘后不做 QMT 补齐。"""
    return now.time() < dtTime(9, 15)


def isOptionContinuousAuction(now: datetime) -> bool:
    """连续竞价才允许空缓存 force：09:30–11:30、13:00–14:57。"""
    t = now.time()
    return (dtTime(9, 30) <= t < dtTime(11, 30)) or (
        dtTime(13, 0) <= t < dtTime(14, 57)
    )


def isOptionCatchupWindow(now: datetime) -> bool:
    """历史兼容入口：盘后 QMT 回捞已停用，避免占用主桥。"""
    return False


def tushareDbConfig() -> dict:
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME_TUSHARE", "stock_tushare"),
        "charset": "utf8mb4",
        "connect_timeout": 5,
        "read_timeout": 15,
        "autocommit": True,
    }


def optBasicListedOnQuery(
    asof, underlyings=OPT_UNDERLYINGS, startDate=None, lookbackDays=None
):
    """近 N 个交易日上市的在市 C/P。未传 startDate 时退日历 fallback。"""
    day = asof if isinstance(asof, date) else date.today()
    if startDate is not None:
        start = startDate if isinstance(startDate, date) else parseQmtExpireDate(startDate)
        if start is None:
            start = optionListedLookbackStart(day)
    elif lookbackDays is not None:
        start = day - timedelta(days=max(0, int(lookbackDays)))
    else:
        start = optionListedLookbackStart(day)
    names = tuple(underlyings or OPT_UNDERLYINGS)
    likes = " OR ".join(["opt_code LIKE %s"] * len(names))
    params = [start, day] + ["%%%s%%" % name for name in names]
    sql = (
        "SELECT ts_code FROM opt_basic "
        "WHERE list_date >= %s AND list_date <= %s "
        "AND delist_date IS NOT NULL AND delist_date >= CURDATE() "
        "AND UPPER(TRIM(COALESCE(call_put,''))) IN ('C','P') "
        "AND (" + likes + ") ORDER BY ts_code"
    )
    return sql, params


def loadSseOpenDates(asof, limitN: int = 40) -> list:
    """asof 及之前的 SSE 开市日。失败返回 []，由日历 fallback 接手。"""
    import pymysql

    day = asof if isinstance(asof, date) else date.today()
    sql = (
        "SELECT cal_date FROM trade_cal "
        "WHERE exchange = 'SSE' AND is_open = 1 AND cal_date <= %s "
        "ORDER BY cal_date DESC LIMIT %s"
    )
    conn = pymysql.connect(**tushareDbConfig())
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (day, max(1, int(limitN))))
            rows = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    out = []
    for raw in rows:
        if isinstance(raw, datetime):
            out.append(raw.date())
        elif isinstance(raw, date):
            out.append(raw)
        else:
            parsed = parseQmtExpireDate(raw)
            if parsed is not None:
                out.append(parsed)
    return out


def loadEtfOptionQmtCodesListedOn(
    asof, underlyings=OPT_QMT_TICK_UNDERLYINGS
) -> list:
    """近 N 个交易日上市 → QMT 代码。库空则 []，不影响拉链新挂。"""
    import pymysql

    day = asof if isinstance(asof, date) else date.today()
    try:
        tradeDates = loadSseOpenDates(day)
    except Exception:
        tradeDates = []
    start = optionListedLookbackStart(day, tradeDates)
    sql, params = optBasicListedOnQuery(asof, underlyings, startDate=start)
    conn = pymysql.connect(**tushareDbConfig())
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    return qmtCodesFromOptBasicRows(rows)


def loadEtfOptionQmtCodes(underlyings=OPT_QMT_TICK_UNDERLYINGS) -> list:
    """回退路径：只读 opt_basic。订全推之后不要再打 QMT 拉链。"""
    import pymysql

    names = tuple(underlyings or OPT_UNDERLYINGS)
    likes = " OR ".join(["opt_code LIKE %s"] * len(names))
    params = ["%%%s%%" % name for name in names]
    sql = (
        "SELECT ts_code FROM opt_basic "
        "WHERE delist_date IS NOT NULL AND delist_date >= CURDATE() "
        "AND UPPER(TRIM(COALESCE(call_put,''))) IN ('C','P') "
        "AND (%s) ORDER BY ts_code" % likes
    )
    conn = pymysql.connect(**tushareDbConfig())
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    return qmtCodesFromOptBasicRows(rows)


def loadAdjustedOptionQmtCodes(underlyings=OPT_QMT_TICK_UNDERLYINGS) -> set:
    """名称尾 A 或 per_unit≠10000 的在市 C/P → QMT 代码。失败由调用方保留旧集。"""
    import pymysql

    names = tuple(underlyings or OPT_UNDERLYINGS)
    likes = " OR ".join(["opt_code LIKE %s"] * len(names))
    params = ["%%%s%%" % name for name in names]
    sql = (
        "SELECT ts_code FROM opt_basic "
        "WHERE delist_date IS NOT NULL AND delist_date >= CURDATE() "
        "AND UPPER(TRIM(COALESCE(call_put,''))) IN ('C','P') "
        "AND (%s) AND ("
        "RIGHT(TRIM(COALESCE(name,'')),1)='A' OR "
        "CAST(NULLIF(TRIM(REPLACE(COALESCE(per_unit,''), ',', '')), '') "
        "AS DECIMAL(18,6)) <> 10000"
        ")"
    ) % likes
    conn = pymysql.connect(**tushareDbConfig())
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    return set(qmtCodesFromOptBasicRows(rows))


def reloadAdjustedOptionCodes(logger=None) -> int:
    try:
        codes = loadAdjustedOptionQmtCodes()
    except Exception as exc:
        if logger is not None:
            logger.warning("调整合约名单失败，沿用旧集: %s", exc)
        return -1
    _setAdjustedOptionCodes(codes)
    if logger is not None:
        logger.info("调整合约轮询垫后 n=%d", len(codes))
    return len(codes)


def loadOptionMetaFromOptBasic(underlyings=OPT_QMT_TICK_UNDERLYINGS) -> dict:
    """盘前用 MySQL 预填 optMeta，避免开盘 728 次 get_option_detail_data。"""
    import pymysql

    names = tuple(underlyings or OPT_UNDERLYINGS)
    likes = " OR ".join(["opt_code LIKE %s"] * len(names))
    params = ["%%%s%%" % name for name in names]
    sql = (
        "SELECT ts_code, call_put, exercise_price, maturity_date, per_unit, opt_code "
        "FROM opt_basic "
        "WHERE delist_date IS NOT NULL AND delist_date >= CURDATE() "
        "AND UPPER(TRIM(COALESCE(call_put,''))) IN ('C','P') "
        "AND (%s)" % likes
    )
    conn = pymysql.connect(**tushareDbConfig())
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    out = {}
    for tsCode, callPut, strike, maturity, perUnit, optCode in rows:
        packed = compactOptionMetaFromBasicRow(
            tsCode, callPut, strike, maturity, perUnit, optCode,
        )
        if packed is None:
            continue
        qmt, meta = packed
        out[qmt] = meta
    return out


def seedOptionMetaFromOptBasic(
    logger=None, underlyings=OPT_QMT_TICK_UNDERLYINGS
) -> int:
    try:
        mapping = loadOptionMetaFromOptBasic(underlyings)
    except Exception as exc:
        if logger is not None:
            logger.warning("optMeta 预填 opt_basic 失败: %s", exc)
        return 0
    with _cacheLock:
        optionMeta.update(mapping)
    if logger is not None:
        logger.info("optMeta 预填 opt_basic n=%d", len(mapping))
    return len(mapping)


def prepareTdxFallbackChain(logger=None) -> list:
    """QMT RPC 未就绪时，仅靠本地 opt_basic 准备 TDX 五链的代码和要素。"""
    if not TDX_OPT_ENABLE:
        return []
    try:
        codes = loadEtfOptionQmtCodes(OPT_UNDERLYINGS)
    except Exception as exc:
        if logger is not None:
            logger.warning("TDX_FALLBACK opt_basic 链加载失败: %s", exc)
        return []
    if not codes:
        if logger is not None:
            logger.warning("TDX_FALLBACK opt_basic 链为空，无法关联 T 型行")
        return []
    _setOptionChainCodes(codes)
    seedOptionMetaFromOptBasic(logger, underlyings=OPT_UNDERLYINGS)
    if logger is not None:
        logger.info("TDX_FALLBACK chain prepared n=%d", len(codes))
    return codes


def pollTdxFallbackOnce(logger=None) -> int:
    """一次无 QMT RPC 的 TDX 合并；异常只影响本轮，下一秒继续。"""
    try:
        bag = fetchTdxOptSnapshot()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        if logger is not None:
            emitCooldownLog(
                logger,
                "tdx_fallback_fetch",
                logging.WARNING,
                "TDX_FALLBACK snapshot unavailable: %s",
                exc,
                cooldownSec=30.0,
            )
        return 0
    return mergeTdxOptionTicks(bag, _copyOptionMeta(), forceTdx=True)


def pollTdxMergeOnce(logger=None, *, forceTdx: bool = False) -> int:
    """一次 TDX HTTP 合并；不访问 QMT RPC，供独立轻量线程调用。"""
    try:
        bag = fetchTdxOptSnapshot()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        if logger is not None:
            emitCooldownLog(
                logger,
                "tdx_merge_fetch",
                logging.INFO,
                "TDX_OPT skip %s",
                exc,
                cooldownSec=30.0,
            )
        return 0
    return mergeTdxOptionTicks(bag, _copyOptionMeta(), forceTdx=forceTdx)


def shouldMergeTdxInOptionPoll() -> bool:
    """独立线程关闭时才在旧的期权轮询循环内合并 TDX。"""
    return bool(TDX_OPT_ENABLE and not TDX_OPT_DEDICATED_MERGE)


def _runTdxMergeLoop(
    logger,
    stopEvent: threading.Event,
    *,
    pollFn=None,
    intervalSec: float | None = None,
) -> None:
    """独立、轻量、start-to-start 的 TDX 合并循环，不依赖 QMT RPC。"""
    poll = pollFn or pollTdxMergeOnce
    interval = (
        TDX_OPT_MERGE_INTERVAL_SEC if intervalSec is None else max(0.0, float(intervalSec))
    )
    lastReportMono = 0.0
    while running and not stopEvent.is_set():
        roundStart = time.monotonic()
        merged = poll(logger, forceTdx=False)
        nowMono = time.monotonic()
        if merged and nowMono - lastReportMono >= TDX_OPT_MERGE_REPORT_SEC:
            logger.info("TDX_OPT dedicated merge n=%d", merged)
            lastReportMono = nowMono
        waitSec = max(0.0, interval - (time.monotonic() - roundStart))
        if waitSec > 0:
            stopEvent.wait(timeout=waitSec)


def _runTdxFallbackLoop(logger, qmtReady: threading.Event) -> None:
    """桥接离线期间保持 TDX-only；QMT 恢复后让出给常规合并链路。"""
    logger.warning(
        "TDX_FALLBACK active: QMT RPC unavailable; serve TDX-only until bridge recovers"
    )
    lastReportMono = 0.0
    while running and not qmtReady.is_set():
        merged = pollTdxFallbackOnce(logger)
        nowMono = time.monotonic()
        if merged and nowMono - lastReportMono >= TDX_FALLBACK_REPORT_SEC:
            logger.info("TDX_FALLBACK merge n=%d", merged)
            lastReportMono = nowMono
        qmtReady.wait(timeout=TDX_FALLBACK_POLL_SEC)
    logger.info("TDX_FALLBACK stop qmt_ready=%s", int(qmtReady.is_set()))


def _sleepInterruptible(totalSec: float) -> None:
    """可中断短睡。remain 先算再 sleep，避免 while 与 sleep 之间时钟前移导致负数。"""
    endTs = time.monotonic() + max(0.0, float(totalSec))
    while running:
        remain = endTs - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(0.5, remain))


def _restartableDaemonLoop(logger, tag: str, fn, *args, backoffSec: float = 5.0) -> None:
    """守护线程/主循环：未预期异常记日志后重启，避免静默挂掉或整进程退出。
    fn 正常 return（通常因 running=False）则结束。"""
    while running:
        try:
            fn(*args)
            return
        except Exception:
            logger.exception("%s 异常，%.0fs 后重启", tag, float(backoffSec))
            _sleepInterruptible(float(backoffSec))


def optTickDownloadStartYmd(asof, lookbackDays: int = OPT_TICK_DL_LOOKBACK_DAYS) -> str:
    """tick 下载起点 YYYYMMDD：从 asof 往前 lookback 个自然日。"""
    day = asof if isinstance(asof, date) else date.today()
    start = day - timedelta(days=max(1, int(lookbackDays)))
    return start.strftime("%Y%m%d")


def optionCodesWithTodayTicks(codes, tickMap, asof) -> list:
    """本地 tick 已是当日的合约。废日再拉 get_option_ticks 也变不成今日量仓。"""
    out = []
    tickMap = tickMap or {}
    for code in codes or []:
        tick = tickMap.get(code)
        if not isinstance(tick, dict):
            continue
        tickDay = decodeQuoteTickDate(
            tick.get("time") or tick.get("stime") or tick.get("t")
        )
        if tickDay == asof:
            out.append(code)
    return out


def optionCodesNeedingTickDownload(codes, tickMap, asof, dailyMap=None) -> list:
    """无可用当日 last/五档的合约；.SZO 优先，同组按日K 成交量降序。
    仅有今日时间戳或 last=0 仍要灌库，避免空 tick 冒充已覆盖。"""
    szo = []
    other = []
    tickMap = tickMap or {}
    dailyMap = dailyMap or {}
    for code in codes or []:
        if optionTickLooksUsable(tickMap.get(code), asof):
            continue
        if str(code).upper().endswith(".SZO"):
            szo.append(code)
        else:
            other.append(code)

    def volOf(code) -> float:
        bar = dailyMap.get(code)
        if not isinstance(bar, dict):
            return 0.0
        v = _asFloat(bar.get("volume") if bar.get("volume") is not None else bar.get("vol"))
        return v if v is not None else 0.0

    szo.sort(key=volOf, reverse=True)
    other.sort(key=volOf, reverse=True)
    return szo + other


def _copyOptionDaily() -> dict:
    with _cacheLock:
        return dict(optionDaily)


def _copyOptionTicks() -> dict:
    with _cacheLock:
        return dict(optionTicks)


def _tickDlTried(asof) -> set:
    """每个交易日只灌一次；失败不记入，以便重试。"""
    global _optionTickDlTried, _optionTickDlAsOf
    if _optionTickDlAsOf != asof:
        _optionTickDlTried = set()
        _optionTickDlAsOf = asof
    return _optionTickDlTried


def _dailyDlTried(asof) -> set:
    """当日 1d 占位柱只灌库一次。"""
    global _optionDailyDlTried, _optionDailyDlAsOf
    if _optionDailyDlAsOf != asof:
        _optionDailyDlTried = set()
        _optionDailyDlAsOf = asof
    return _optionDailyDlTried


def optionCodesNeedingDailyDownload(codes, dailyMap, asof) -> list:
    """没有当日非占位 1d 的合约；.SZO 优先。"""
    szo = []
    other = []
    dailyMap = dailyMap or {}
    for code in codes or []:
        bar = dailyMap.get(code)
        if (
            isinstance(bar, dict)
            and bar.get("d") == asof
            and not optionDailyBarLooksStub(bar)
        ):
            continue
        if str(code).upper().endswith(".SZO"):
            szo.append(code)
        else:
            other.append(code)
    return szo + other


def _acquireBackgroundRpcLock(waitSliceSec: float = 0.05) -> bool:
    """低优先级补齐等待锁时可被 whole-quote 恢复撤回。

    已进入外部 RPC 的调用无法由 Python 安全中断；调用方仍需时间门闩避免
    长请求跨越 09:15。本函数解决排队竞态：watchdog 置 ``_quoteHold`` 后，
    后台请求不得在锁释放瞬间抢在重订前执行。
    """
    waitSec = max(0.01, float(waitSliceSec))
    while running:
        if _quoteHold.is_set():
            return False
        if not _rpcLock.acquire(timeout=waitSec):
            continue
        if _quoteHold.is_set():
            _rpcLock.release()
            return False
        return True
    return False


def _downloadOptionPeriodStore(logger, xtdata, codes, startYmd: str, period: str, timeoutSec: float) -> int:
    """灌指定周期本地库。"""
    codes = [str(c) for c in (codes or []) if str(c).strip()]
    if not codes:
        return 0
    wall = datetime.now()
    if wall.time() < dtTime(9, 15) and not shouldStartPreopenRpc(wall, timeoutSec):
        return 0
    client = getattr(xtdata, "client", None)
    if client is None or not hasattr(client, "call"):
        logger.warning("OPT_DL 无 RPC client period=%s", period)
        return 0
    gotLock = _acquireBackgroundRpcLock()
    if not gotLock:
        return 0
    try:
        try:
            response = client.call(
                "download_history_data2",
                {
                    "stock_list": codes,
                    "period": period,
                    "start_time": startYmd,
                    "end_time": "",
                    "incrementally": True,
                },
                timeout_seconds=float(timeoutSec),
            )
        finally:
            _rpcLock.release()
        if response is False:
            logger.warning(
                "OPT_DL rejected period=%s n=%d first=%s",
                period,
                len(codes),
                codes[0],
            )
            return 0
        return len(codes)
    except Exception as exc:
        logger.warning(
            "OPT_DL fail period=%s n=%d first=%s %s",
            period,
            len(codes),
            codes[0],
            exc,
        )
        return 0


def _downloadOptionTickStore(logger, xtdata, codes, startYmd: str) -> int:
    """只灌 QMT 本地 tick 库，不把全历史拉进客户端缓存。"""
    return _downloadOptionPeriodStore(
        logger, xtdata, codes, startYmd, "tick", OPT_TICK_DL_TIMEOUT_SEC,
    )


def _downloadOptionDailyStore(logger, xtdata, codes, startYmd: str) -> int:
    """灌当日 1d。创业板期权本地日K 常停在昨收占位柱，不灌则量仓全是 0。"""
    codes = [str(c) for c in (codes or []) if str(c).strip()]
    if not codes:
        return 0
    wall = datetime.now()
    if wall.time() < dtTime(9, 15) and not shouldStartPreopenRpc(
        wall, OPT_DAILY_DL_TIMEOUT_SEC
    ):
        return 0
    client = getattr(xtdata, "client", None)
    if client is None or not hasattr(client, "call"):
        logger.warning("OPT_DAILY_DL 无 RPC client")
        return 0
    gotLock = _acquireBackgroundRpcLock()
    if not gotLock:
        return 0
    try:
        try:
            client.call(
                "download_history_data2",
                {
                    "stock_list": codes,
                    "period": "1d",
                    "start_time": startYmd,
                    "end_time": "",
                },
                timeout_seconds=OPT_DAILY_DL_TIMEOUT_SEC,
            )
        finally:
            _rpcLock.release()
        return len(codes)
    except Exception as exc:
        logger.warning(
            "OPT_DAILY_DL fail n=%d first=%s %s",
            len(codes),
            codes[0],
            exc,
        )
        return 0


def _maybeDownloadStubOptionDaily(logger, xtdata, codes, asof, includeSzo: bool = False) -> list:
    """盘中只灌 1d 占位合约。codes 已由闸门筛过；includeSzo=False 时再剔创业板。"""
    universe = list(codes or []) if includeSzo else optionShoCodes(codes)
    tried = _dailyDlTried(asof)
    need = [
        c
        for c in optionCodesNeedingDailyDownload(universe, _copyOptionDaily(), asof)
        if c not in tried
    ]
    if not need:
        return []
    chunk = need[:OPT_DAILY_DL_CHUNK]
    n = _downloadOptionDailyStore(logger, xtdata, chunk, optTickDownloadStartYmd(asof))
    if n > 0:
        tried.update(chunk)
    logger.info(
        "OPT_DAILY_DL n=%d first=%s remain=%d",
        n,
        chunk[0] if chunk else "",
        max(0, len(need) - (len(chunk) if n > 0 else 0)),
    )
    return chunk if n > 0 else []


def _maybeDownloadSzoDailyStore(logger, xtdata, codes, asof) -> list:
    """灌创业板当日 1d。仅订阅 RPC 不可用时允许；订满/灌码中禁止。"""
    if not _szoSubUnavailable:
        return []
    chunk = optionSzoCodes(codes)[:OPT_DAILY_DL_CHUNK]
    if not chunk:
        return []
    startYmd = asof.strftime("%Y%m%d") if hasattr(asof, "strftime") else str(asof).replace("-", "")[:8]
    n = _downloadOptionDailyStore(logger, xtdata, chunk, startYmd)
    logger.info(
        "OPT_SZO_DAILY_DL n=%d first=%s",
        n,
        chunk[0] if chunk else "",
    )
    return chunk if n > 0 else []


def _seedListedTodayFromOptBasic(logger, asof) -> int:
    """近 4 个交易日上市名单并入新挂集合；周末后仍覆盖周四挂牌。"""
    try:
        codes = loadEtfOptionQmtCodesListedOn(asof)
    except Exception as exc:
        if logger is not None:
            logger.warning("OPT_LISTED_BASIC fail %s", exc)
        return 0
    rememberOptionChainAdds(codes, asof)
    if codes and logger is not None:
        logger.info("OPT_LISTED_BASIC n=%d first=%s", len(codes), codes[0])
    return len(codes)


def _maybeDownloadListedTodayDaily(logger, xtdata, asof) -> list:
    """定期灌今日新挂 1d，解开有量冻结柱；不占用 stub 的当日只灌一次集合。"""
    global _listedDailyDlCursor
    need = optionLiveListedDailyDownloadCodes(optionListedTodayCodes(asof))
    chunk, nxt = optionListedDailyDownloadChunk(
        need, _listedDailyDlCursor, OPT_LISTED_DAILY_DL_CHUNK
    )
    if not chunk:
        return []
    n = _downloadOptionDailyStore(
        logger, xtdata, chunk, optionDailyDownloadStartYmd(asof)
    )
    if n > 0:
        _listedDailyDlCursor = nxt
        _refreshOptionDailyBars(logger, xtdata, chunk)
    logger.info(
        "OPT_LISTED_DAILY_DL n=%d chunk=%d listed=%d first=%s",
        n,
        len(chunk),
        len(need),
        chunk[0],
    )
    return chunk if n > 0 else []


def _maybeDownloadSzoLiveTicks(logger, xtdata, codes, asof) -> list:
    """盘中灌创业板当日 1m，合成量仓写入 optionTicks；不走 get_option_ticks。"""
    szo = optionSzoCodes(codes)
    if not szo:
        return []
    ymd = szoLiveTickDownloadStartYmd(asof)
    t0 = time.perf_counter()
    n = 0
    for chunk in chunkCodes(szo, OPT_SZO_MIN_DL_CHUNK):
        if not running:
            break
        n += _downloadOptionPeriodStore(
            logger, xtdata, chunk, ymd,
            OPT_SZO_MIN_PERIOD, OPT_SZO_MIN_DL_CHUNK_TIMEOUT_SEC,
        )
    if n <= 0:
        logger.info("OPT_SZO_MIN_DL skip n=0 szo=%d", len(szo))
        return []
    applied = _applySzoMinuteQuotes(logger, xtdata, szo, asof, ymd)
    logger.info(
        "OPT_SZO_MIN_DL n=%d applied=%d elapsed_s=%.1f first=%s",
        n,
        applied,
        time.perf_counter() - t0,
        szo[0],
    )
    return szo


def _applySzoMinuteQuotes(
    logger, xtdata, codes, asof, ymd: str, rpcFallback: bool = True,
    observedAt: datetime | None = None,
) -> int:
    """读 1m 合成 L1 写入 optionTicks。直播窗 rpcFallback=False：只读本地 DAT。

    download 已把当日 1m 写进 <datadir>/SZO/60/*.DAT，再用 RPC 传回来是白跑一趟：
    132 张要 7 次往返且全程占 _rpcLock；本地读同样 132 张约 8ms。
    """
    ok = 0
    oiApplied = 0
    wanted = [str(c).strip() for c in (codes or []) if str(c).strip()]
    if not wanted:
        return 0
    localDone = set()
    observedAtMs = (
        observedAt.timestamp() * 1000.0
        if isinstance(observedAt, datetime)
        else datetime.now().timestamp() * 1000.0
    )
    localBars = readQmtLocalBars(wanted, OPT_SZO_MIN_PERIOD, QMT_DAT_TAIL_MIN)
    if localBars:
        # DAT 解析放在锁外；影子统计只读快照，不改变正式 OI。
        localMinuteTicks = {}
        localOiTicks = {}
        for code in wanted:
            records = localBars.get(code)
            tick = szoMinuteRecordsToTick(records, asof)
            if tick:
                localMinuteTicks[code] = tick
            oiTick = szoMinuteRecordsToOiCandidate(
                records, asof, observedAtMs=observedAtMs
            )
            if oiTick:
                localOiTicks[code] = oiTick
        if OPT_SZO_OI_SHADOW_ENABLE and localOiTicks:
            with _cacheLock:
                shadowExisting = {
                    code: dict(optionTicks.get(code) or {})
                    for code in localOiTicks
                }
            shadow = szoMinuteOiShadowStats(shadowExisting, localOiTicks)
            emitCooldownLog(
                logger,
                "szo_oi_shadow",
                logging.INFO,
                "OPT_SZO_OI_SHADOW observed=%d newer=%d mismatch=%d "
                "candidate=%d missing=%d",
                shadow["observed"],
                shadow["newer"],
                shadow["mismatch"],
                shadow["candidate"],
                shadow["missing_existing"],
                cooldownSec=OPT_SZO_OI_SHADOW_REPORT_SEC,
            )
        with _cacheLock:
            spotByUndl = etfSpotByUndlSix()
            for code in wanted:
                tick = localMinuteTicks.get(code)
                oiTick = localOiTicks.get(code)
                if tick is not None:
                    localDone.add(code)
                if tick is None and oiTick is None:
                    continue
                existing = optionTicks.get(code)
                applied = False
                oiOverlaid = overlaySzoMinuteOiCandidate(existing, oiTick)
                if oiOverlaid is not None:
                    optionTicks[code] = oiOverlaid
                    existing = oiOverlaid
                    applied = True
                    oiApplied += 1
                meta = optionMeta.get(code)
                six = undlSixFromMeta(meta) or "159915"
                spot = spotByUndl.get(six)
                if tick is not None and shouldApplySzoMinuteTick(
                    existing, tick, asof, meta, spot
                ):
                    quoteTick = dict(tick)
                    for key in ("oi", "openInt", "openInterest", "oiTime", "oi_time"):
                        quoteTick.pop(key, None)
                    overlaid = overlaySzoMinuteTick(existing, quoteTick)
                    if overlaid is not None:
                        if oiOverlaid is not None:
                            for key in (
                                "oi", "openInt", "openInterest", "oiTime",
                                "oiChangedTime", "oiSource",
                            ):
                                if key in oiOverlaid:
                                    overlaid[key] = oiOverlaid[key]
                        optionTicks[code] = overlaid
                        applied = True
                if applied:
                    ok += 1
    missing = [c for c in wanted if c not in localDone]
    if (not rpcFallback) or (not missing):
        if wanted:
            logger.info(
                "OPT_SZO_MIN_LOCAL n=%d applied=%d miss=%d rpc=%s",
                len(wanted),
                ok,
                len(missing),
                int(bool(rpcFallback)),
            )
            if oiApplied:
                logger.info("OPT_SZO_OI_APPLY n=%d source=qmt_1m", oiApplied)
        if ok:
            with _cacheLock:
                optionStats["lastWall"] = datetime.now()
                optionStats["lastPushMono"] = time.monotonic()
            invalidateQuoteSnapshotJson()
        return ok
    logger.info(
        "OPT_SZO_MIN_READ local=%d rpc_fallback=%d", len(localDone), len(missing)
    )
    for chunk in chunkCodes(missing, OPT_SZO_MIN_READ_CHUNK):
        if not running:
            break
        gotLock = _acquireBackgroundRpcLock()
        if not gotLock:
            break
        try:
            try:
                data = xtdata.get_market_data_ex(
                    field_list=["close", "volume", "openInterest", "time"],
                    stock_list=list(chunk),
                    period=OPT_SZO_MIN_PERIOD,
                    start_time=ymd,
                    end_time="",
                    count=OPT_SZO_MIN_BAR_COUNT,
                    fill_data=False,
                    timeout_seconds=15.0,
                ) or {}
            finally:
                _rpcLock.release()
        except Exception as exc:
            logger.warning("OPT_SZO_MIN_READ fail n=%d first=%s %s", len(chunk), chunk[0], exc)
            data = {}
        if not isinstance(data, dict):
            continue
        with _cacheLock:
            spotByUndl = etfSpotByUndlSix()
            for code in chunk:
                records = optionFrameToRecords(data.get(code))
                tick = szoMinuteRecordsToTick(records, asof)
                oiTick = szoMinuteRecordsToOiCandidate(
                    records, asof, observedAtMs=observedAtMs
                )
                if tick is None and oiTick is None:
                    continue
                existing = optionTicks.get(code)
                applied = False
                oiOverlaid = overlaySzoMinuteOiCandidate(existing, oiTick)
                if oiOverlaid is not None:
                    optionTicks[code] = oiOverlaid
                    existing = oiOverlaid
                    applied = True
                    oiApplied += 1
                meta = optionMeta.get(code)
                six = undlSixFromMeta(meta) or "159915"
                spot = spotByUndl.get(six)
                if tick is not None:
                    if not shouldApplySzoMinuteTick(
                        existing, tick, asof, meta, spot
                    ):
                        if existing and not tickHasFiveBand(existing):
                            last = existing.get("lastPrice") or existing.get("last")
                            if optionPremiumLooksImplausible(last, meta, spot):
                                optionTicks.pop(code, None)
                    else:
                        quoteTick = dict(tick)
                        for key in (
                            "oi", "openInt", "openInterest", "oiTime", "oi_time",
                        ):
                            quoteTick.pop(key, None)
                        overlaid = overlaySzoMinuteTick(existing, quoteTick)
                        if overlaid is not None:
                            if oiOverlaid is not None:
                                for key in (
                                    "oi", "openInt", "openInterest", "oiTime",
                                    "oiChangedTime", "oiSource",
                                ):
                                    if key in oiOverlaid:
                                        overlaid[key] = oiOverlaid[key]
                            optionTicks[code] = overlaid
                            applied = True
                if applied:
                    ok += 1
    if oiApplied:
        logger.info("OPT_SZO_OI_APPLY n=%d source=qmt_1m", oiApplied)
    if ok:
        with _cacheLock:
            optionStats["lastWall"] = datetime.now()
            optionStats["lastPushMono"] = time.monotonic()
        invalidateQuoteSnapshotJson()
    return ok


def _syncOptionQuoteSubscriptions(
    logger, xtdata, wanted, force: bool = False, staleSec: float | None = None,
    healUncached: bool = False,
) -> dict | None:
    """同步沪市期权 subscribe_quote(tick)，配额 OPT_QUOTE_SUB_MAX_OWNED。"""
    global _szoSubOwned, _szoSubUnavailable, _szoSubRetryMono, _szoSubLastSyncMono
    global _optSubRevisionRefuseLogged
    if shouldRefuseOptionSubForRevision(_bridgeRevisionOk):
        if not _optSubRevisionRefuseLogged:
            logger.error(
                "OPT_SUB skip revision_mismatch got=%s want=%s",
                _bridgeRevision or "-",
                EXPECTED_RPC_REVISION,
            )
            _optSubRevisionRefuseLogged = True
        return None
    wanted = optionShoCodes(wanted)
    client = getattr(xtdata, "client", None)
    if client is None or not hasattr(client, "call"):
        _szoSubUnavailable = True
        _szoSubRetryMono = time.monotonic() + OPT_SZO_SUB_RETRY_SEC
        logger.warning("OPT_SUB 无 RPC client")
        return None
    payload = {
        "codes": wanted,
        "period": OPT_SZO_SUB_PERIOD,
        "dividend_type": "none",
        "add_limit": OPT_SZO_SUB_ADD_LIMIT,
        "max_owned": OPT_QUOTE_SUB_MAX_OWNED,
    }
    if force:
        payload["force"] = True
    if healUncached:
        payload["heal_uncached"] = True
    if staleSec is not None:
        payload["stale_sec"] = float(staleSec)
    gotLock = _acquireBackgroundRpcLock()
    if not gotLock:
        return None
    try:
        try:
            result = client.call(
                "sync_single_quote",
                payload,
                timeout_seconds=OPT_SZO_SUB_TIMEOUT_SEC,
            )
        finally:
            _rpcLock.release()
    except Exception as exc:
        if isSzoSubRpcUnavailable(exc):
            _szoSubUnavailable = True
            _szoSubRetryMono = time.monotonic() + OPT_SZO_SUB_RETRY_SEC
        logger.warning("OPT_SUB fail %s", exc)
        return None
    if not isinstance(result, dict):
        logger.warning("OPT_SUB bad result %s", type(result).__name__)
        return None
    _szoSubUnavailable = False
    _szoSubOwned = applySzoSubSyncLocal(_szoSubOwned, result)
    _szoSubLastSyncMono = time.monotonic()
    pendingN = len(result.get("pending") or [])
    failedN = len(result.get("failed") or [])
    logger.info(
        "OPT_SUB added=%d dropped=%d kept=%d pending=%d failed=%d "
        "owned=%d wanted=%d cached=%s force=%s forced=%s heal_uncached=%s",
        len(result.get("added") or []),
        len(result.get("dropped") or []),
        len(result.get("kept") or []),
        pendingN,
        failedN,
        len(_szoSubOwned),
        len(wanted),
        result.get("cached"),
        bool(force),
        result.get("forced"),
        bool(result.get("heal_uncached") or healUncached),
    )
    if pendingN and running:
        _sleepInterruptible(OPT_SZO_SUB_YIELD_SEC)
    return result


def _applyCachedOptionTicks(ticks) -> int:
    """桥接回调缓存写入 optionTicks。"""
    if not isinstance(ticks, dict) or not ticks:
        return 0
    asof = date.today()
    ok = 0
    with _cacheLock:
        for code, tick in ticks.items():
            if not isinstance(tick, dict):
                continue
            existing = optionTicks.get(code)
            if (
                isinstance(existing, dict)
                and existing.get("fromTdx")
                and not tick.get("fromTdx")
                and optionTickLooksUsable(existing, asof)
            ):
                patched = optionTickFillQtyFromIncoming(existing, tick)
                if patched is not None:
                    optionTicks[code] = patched
                    ok += 1
                continue
            if not optionTickShouldReplace(existing, tick, asof):
                patched = optionTickFillQtyFromIncoming(existing, tick)
                if patched is not None:
                    optionTicks[code] = patched
                    ok += 1
                continue
            merged = optionTickMergeKeepQty(existing, tick)
            if not optionTickPayloadChanged(existing, merged):
                continue
            optionTicks[code] = merged
            ok += 1
        if ok:
            optionStats["lastWall"] = datetime.now()
            optionStats["lastPushMono"] = time.monotonic()
    if ok:
        invalidateQuoteSnapshotJson()
    return ok


def filterOptionQuoteCacheTicks(
    ticks, *, now: datetime | None = None, maxAgeSec: float = OPT_QUOTE_CACHE_MAX_AGE_SEC,
) -> tuple[dict, int]:
    """丢弃「刚收到回调、但交易所事件已冻结」的订阅缓存。

    桥接缓存的 ``ageSec`` 仅代表 Python 收到 callback 的时间。QMT 在订阅冻结时
    会重复回放旧 L1；若只看 receipt age，调用方会把 40 分钟前的 ``openInt`` 当成
    2 秒前的实时 OI，永远不会触发订阅自愈。这里以 tick 自带的交易所时间为准；
    时间缺失/不可解析也不能证明实时，故按 stale 处理。
    """
    if not isinstance(ticks, dict):
        return {}, 0
    wall = now if isinstance(now, datetime) else datetime.now()
    nowMs = wall.timestamp() * 1000.0
    asof = wall.date()
    try:
        maxAgeMs = max(0.0, float(maxAgeSec)) * 1000.0
    except (TypeError, ValueError):
        maxAgeMs = 0.0
    kept = {}
    staleN = 0
    for code, tick in ticks.items():
        if not isinstance(tick, dict):
            staleN += 1
            continue
        eventMs = optionTickTimeMs(tick)
        eventDay = decodeQuoteTickDate(eventMs)
        if (
            eventMs <= 0
            or eventDay != asof
            or nowMs - eventMs > maxAgeMs
        ):
            staleN += 1
            continue
        kept[code] = tick
    return kept, staleN


def optionQuoteCacheErrorOutcome(exc) -> str:
    """将桥接读取失败分类；任何失败都不得伪装成权威空缓存。"""
    if isinstance(exc, TimeoutError):
        return "timeout"
    text = f"{type(exc).__name__} {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "busy" in text:
        return "busy"
    return "error"


def _pollOptionQuoteCache(
    logger, xtdata, maxAgeSec=None, *, returnOutcome: bool = False,
):
    """读 subscribe_quote 回调缓存，可选返回权威性状态。"""
    ageSec = OPT_QUOTE_CACHE_MAX_AGE_SEC if maxAgeSec is None else float(maxAgeSec)
    getter = getattr(xtdata, "get_single_quote_ticks", None)
    client = getattr(xtdata, "client", None)
    outcome = "ok"
    gotLock = _acquireBackgroundRpcLock()
    if not gotLock:
        if returnOutcome:
            return {}, "busy"
        return {}
    try:
        try:
            if callable(getter):
                data = getter(max_age_sec=ageSec, timeout_seconds=3.0)
            elif client is not None and hasattr(client, "call"):
                data = client.call(
                    "get_single_quote_ticks",
                    {"max_age_sec": ageSec},
                    timeout_seconds=3.0,
                )
            else:
                data = {}
                outcome = "transport_unknown"
        finally:
            _rpcLock.release()
    except Exception as exc:
        outcome = optionQuoteCacheErrorOutcome(exc)
        logger.info("OPT_QUOTE_CACHE skip outcome=%s %s", outcome, exc)
        data = {}
    if not isinstance(data, dict):
        data = {}
        outcome = "error"
    rawN = len(data)
    data, staleN = filterOptionQuoteCacheTicks(data, maxAgeSec=ageSec)
    if outcome == "ok" and not data:
        outcome = "authoritative_empty"
    hit = _applyCachedOptionTicks(data)
    szoN = sum(1 for c in data if str(c).upper().endswith(".SZO"))
    shoN = sum(1 for c in data if str(c).upper().endswith(".SHO"))
    logger.info(
        "OPT_QUOTE_CACHE raw=%d stale_event=%d hit=%d szo=%d sho=%d outcome=%s",
        rawN, staleN, hit, szoN, shoN, outcome,
    )
    if returnOutcome:
        return data, outcome
    return data


def _maybeDownloadStaleOptionTicks(logger, xtdata, codes, asof) -> int:
    """盘前按固定小片灌 tick，禁止整条标的链长期占用共享 RPC 锁。"""
    tried = _tickDlTried(asof)
    need = [
        c
        for c in optionCodesNeedingTickDownload(
            codes, _copyOptionTicks(), asof, _copyOptionDaily()
        )
        if c not in tried
    ]
    if not need:
        return 0
    needSet = set(need)
    groups = groupOptionCodesByUnderlying(codes, _copyOptionMeta())
    chunk = []
    for group in groups:
        chunk = [c for c in group if c in needSet]
        if chunk:
            break
    if not chunk:
        chunk = need
    chunk = optionTickGapDownloadChunk(chunk)
    n = _downloadOptionTickStore(logger, xtdata, chunk, optTickDownloadStartYmd(asof))
    if n > 0:
        tried.update(chunk)
    logger.info(
        "OPT_TICK_DL n=%d first=%s remain=%d",
        n,
        chunk[0] if chunk else "",
        max(0, len(need) - (len(chunk) if n > 0 else 0)),
    )
    return n


def _maybeDownloadLiveStaleOptionTicks(
    logger, xtdata, codes, asof, uniqueN, silenceSec,
) -> int:
    """直播窗：全推健康后灌今日沪市 tick；过期盘口走增量小片，避免假活。"""
    global _optionTickRefreshMono
    if not isWholeQuoteHealthyForOptDl(uniqueN, silenceSec):
        return 0
    now = datetime.now()
    nowMono = time.monotonic()
    ticks = _copyOptionTicks()
    sho = optionShoCodes(codes)
    tried = _tickDlTried(asof)
    cold = [
        c
        for c in optionCodesNeedingTickDownload(sho, ticks, asof, _copyOptionDaily())
        if c not in tried
    ]
    coldSet = set(cold)
    stale = [
        c
        for c in optionCodesNeedingLiveTickRefresh(sho, ticks, asof, now)
        if c not in coldSet
        and optionTickRefreshReady(
            c, nowMono, _optionTickRefreshMono, OPT_LIVE_REFRESH_COOLDOWN_SEC
        )
    ]
    if cold:
        take = OPT_LIVE_DL_CHUNK
        need = cold + interleaveOptionCodesByUnderlying(stale, _copyOptionMeta())
    else:
        take = optionLiveRefreshChunkSize(uniqueN, silenceSec)
        with _cacheLock:
            rotate = int(optionStats.get("rounds") or 0)
        need = optionLiveRefreshPriorityChunk(
            stale, rotate, take, ticks, _copyOptionMeta(), _copyOptionDaily(),
        )
    if not need:
        return 0
    chunk = need[:take]
    n = _downloadOptionPeriodStore(
        logger,
        xtdata,
        chunk,
        szoLiveTickDownloadStartYmd(asof),
        "tick",
        OPT_LIVE_DL_TIMEOUT_SEC,
    )
    pollOk = 0
    usableN = 0
    if n > 0:
        for part in chunkCodes(chunk, OPT_CHUNK_SIZE):
            partOk, _fail, _ms = _pollOneOptionChunk(
                logger, xtdata, part, tuple(part[:3]), 0,
            )
            pollOk += partOk
        ticks = _copyOptionTicks()
        for code in chunk:
            if code in coldSet:
                tried.add(code)
            _optionTickRefreshMono[code] = nowMono
            if optionTickLooksUsable(ticks.get(code), asof):
                usableN += 1
    logger.info(
        "OPT_TICK_DL live n=%d cold=%d stale=%d poll_ok=%d usable=%d "
        "first=%s remain=%d unique=%d silence_s=%.1f",
        n,
        len(cold),
        len(stale),
        pollOk,
        usableN,
        chunk[0] if chunk else "",
        max(0, len(need) - len(chunk)),
        uniqueN,
        silenceSec,
    )
    return n


def _maybeDownloadSzoLiveTicksChunk(
    logger, xtdata, codes, asof, uniqueN, silenceSec,
) -> int:
    """直播窗：全推健康后小片增量灌创业板当日 1m；无盘口 90s 冷却，避免空转。"""
    global _szoLiveDlMono, _szoLiveRoundMono, _szoLiveRoundPhase
    if not isWholeQuoteHealthyForOptDl(uniqueN, silenceSec):
        return 0
    nowMono = time.monotonic()
    nowWall = datetime.now()
    started = time.perf_counter()
    ticks = _copyOptionTicks()
    need = optionSzoLiveDownloadCodes(
        codes, ticks, asof, nowWall, _szoLiveDlMono, nowMono,
    )
    if not optionSzoLiveShouldDownload(
        need, ticks, asof, nowMono, _szoLiveRoundMono,
        nowWall=nowWall,
        lastPhase=_szoLiveRoundPhase,
        phaseAlign=OPT_SZO_MIN_PHASE_ALIGN_ENABLE,
    ):
        return 0
    chunk = need[:OPT_LIVE_REFRESH_CHUNK]
    phaseKey = optionSzoLivePhaseKey(nowWall)
    # 相位在发请求前消费；即使桥接失败也不在同一相位高频重试。
    if OPT_SZO_MIN_PHASE_ALIGN_ENABLE and phaseKey:
        _szoLiveRoundPhase = phaseKey
    ymd = szoLiveTickDownloadStartYmd(asof)
    n = _downloadOptionPeriodStore(
        logger,
        xtdata,
        chunk,
        ymd,
        OPT_SZO_MIN_PERIOD,
        OPT_LIVE_DL_TIMEOUT_SEC,
    )
    applied = 0
    if n > 0:
        applied = _applySzoMinuteQuotes(
            logger, xtdata, chunk, asof, ymd, rpcFallback=False,
        )
        for code in chunk:
            _szoLiveDlMono[code] = nowMono
        _szoLiveRoundMono = nowMono
    logger.info(
        "OPT_SZO_MIN_DL live n=%d applied=%d remain=%d elapsed_s=%.3f unique=%d silence_s=%.1f "
        "phase=%s aligned=%d fail_retry=%s",
        n,
        applied,
        max(0, len(need) - len(chunk)),
        time.perf_counter() - started,
        uniqueN,
        silenceSec,
        phaseKey or "cold_immediate",
        int(OPT_SZO_MIN_PHASE_ALIGN_ENABLE),
        "next_phase" if phaseKey else "cold_path",
    )
    return n


def _maybeDownloadOptionTickGaps(logger, xtdata, codes, asof) -> list:
    """直播窗只灌当日新挂且无 tick 的小片，禁止把重启空缓存当成整链缺口。"""
    tried = _tickDlTried(asof)
    need = optionLiveTickGapCodes(
        [
            c
            for c in optionCodesNeedingTickDownload(
                codes, _copyOptionTicks(), asof, _copyOptionDaily()
            )
            if c not in tried
        ],
        optionListedTodayCodes(asof),
    )
    chunk = optionTickGapDownloadChunk(need)
    if not chunk:
        return []
    n = _downloadOptionTickStore(logger, xtdata, chunk, optTickDownloadStartYmd(asof))
    if n > 0:
        tried.update(chunk)
    logger.info(
        "OPT_TICK_GAP n=%d first=%s remain=%d listed=%d",
        n,
        chunk[0] if chunk else "",
        max(0, len(need) - (len(chunk) if n > 0 else 0)),
        len(optionListedTodayCodes(asof)),
    )
    return chunk if n > 0 else []


def _refreshOptionDailyWithGates(
    logger, xtdata, codes, asof, szoHealthy, szoUnavailable,
    owned=None, listedToday=None, rotate: int = 0,
) -> int:
    """按订阅状态选择日 K 宇宙；订未满仍灌沪市占位柱。"""
    global _lastDailyGateMode
    mode = optionDailyGateMode(codes, szoHealthy, szoUnavailable)
    pollCodes = optionDailyPollCodes(
        codes, szoHealthy, szoUnavailable, owned, listedToday
    )
    pollCodes = orderOptionCodesForPoll(
        pollCodes,
        _copyOptionTicks(),
        _copyOptionDaily(),
        _copyAdjustedOptionCodes(),
        _copyOptionMeta(),
        listedSet=set(listedToday or ()),
    )
    stubUniverse = optionStubDailyDownloadCodes(codes, szoHealthy, szoUnavailable)
    if mode != _lastDailyGateMode:
        logger.info(
            "OPT_DAILY_GATE mode=%s poll=%d stub_n=%d szo=%d",
            mode,
            len(pollCodes),
            len(stubUniverse),
            len(optionSzoCodes(codes)),
        )
        _lastDailyGateMode = mode
    dailyN = _refreshOptionDailyBars(logger, xtdata, pollCodes, rotate=rotate)
    if not stubUniverse:
        return dailyN
    stubCodes = _maybeDownloadStubOptionDaily(
        logger,
        xtdata,
        stubUniverse,
        asof,
        includeSzo=True,
    )
    if stubCodes:
        dailyN += _refreshOptionDailyBars(logger, xtdata, stubCodes, rotate=rotate)
    return dailyN


def _refreshOptionDailyBars(logger, xtdata, codes, rotate: int = 0) -> int:
    """按标的拉当日 1d 量仓；盘中 T 型主要靠这条，不靠过期 tick。rotate 轮转五链。"""
    t0 = time.perf_counter()
    ok = 0
    stubSkip = 0
    asof = date.today()
    sliced = optionPollSlices(codes, _copyOptionMeta(), rotate)
    wanted = [code for chunk in sliced for code in chunk]
    localDone = set()
    localBars = readQmtLocalBars(wanted, "1d", QMT_DAT_TAIL_DAY)
    if localBars:
        with _cacheLock:
            for code in wanted:
                bar = optionDailyBarFromRecords(localBars.get(code), asof=asof)
                if bar is None:
                    continue
                localDone.add(code)
                optionDaily[code] = bar
                ok += 1
    chunks = [
        [code for code in chunk if code not in localDone] for chunk in sliced
    ]
    chunks = [chunk for chunk in chunks if chunk]
    for i, chunk in enumerate(chunks):
        if not running:
            break
        wall = datetime.now()
        if wall.time() < dtTime(9, 15) and not shouldStartPreopenRpc(
            wall, OPT_DAILY_RPC_TIMEOUT_SEC
        ):
            break
        gotLock = _acquireBackgroundRpcLock()
        if not gotLock:
            break
        try:
            try:
                data = xtdata.get_market_data_ex(
                    # 禁止空 field_list：空表会走 RPC 返回全 11 列（桥实测 ~0.97s/批），
                    # 648 张日线轮询会把桥的 RPC 队列打满，连 ping/全推订阅都排不上
                    # （2026-09-11 现场：全推 unique 卡在个位数，根因即此）。
                    # 显式 OHLCV 列可走 FormulaServer 快路径（实测 ~0.03s，约 30x）。
                    field_list=OPT_DAILY_FIELDS,
                    stock_list=list(chunk),
                    period="1d",
                    count=2,
                    fill_data=False,
                    timeout_seconds=OPT_DAILY_RPC_TIMEOUT_SEC,
                ) or {}
            finally:
                _rpcLock.release()
        except Exception as exc:
            first = chunk[0] if chunk else ""
            logger.warning("OPT_DAILY fail n=%d first=%s %s", len(chunk), first, exc)
            data = {}
        if isinstance(data, dict):
            with _cacheLock:
                for code in chunk:
                    bar = optionDailyBarFromFrame(data.get(code), asof=asof)
                    if bar is None:
                        stubSkip += 1
                        continue
                    optionDaily[code] = bar
                    ok += 1
        if running and i + 1 < len(chunks):
            _sleepInterruptible(OPT_CHUNK_YIELD_SEC)
    logger.info(
        "OPT_DAILY ok=%d local=%d stub_skip=%d chunks=%d elapsed_s=%.1f",
        ok,
        len(localDone),
        stubSkip,
        len(chunks),
        time.perf_counter() - t0,
    )
    if ok:
        with _cacheLock:
            optionStats["lastWall"] = datetime.now()
            optionStats["lastPushMono"] = time.monotonic()
        invalidateQuoteSnapshotJson()
    return ok


# ETF 日K 只要 OHLCV；显式 field_list 走 FormulaServer 快路径，避免空列表打 RPC 并刷 bigqmt 提示日志
ETF_DAILY_FIELD_LIST = ["open", "high", "low", "close", "volume", "amount"]


def _refreshEtfDailyBars(logger, xtdata) -> int:
    """五只 ETF 当日 1d；QMT 占位柱则回退 rt_k。"""
    codes = list(OPT_UNDERLYING_QMT)
    asof = date.today()
    gotLock = _acquireBackgroundRpcLock()
    if not gotLock:
        return 0
    try:
        try:
            data = xtdata.get_market_data_ex(
                field_list=list(ETF_DAILY_FIELD_LIST),
                stock_list=codes,
                period="1d",
                count=2,
                fill_data=False,
                timeout_seconds=OPT_DAILY_RPC_TIMEOUT_SEC,
            ) or {}
        finally:
            _rpcLock.release()
    except Exception as exc:
        logger.warning("ETF_DAILY fail %s", exc)
        data = {}
    ok = 0
    if isinstance(data, dict):
        with _cacheLock:
            for code in codes:
                bar = optionDailyBarFromFrame(data.get(code))
                if bar is None:
                    continue
                etfDaily[code] = bar
                ok += 1
    rtkN = 0
    if not isEtfContinuousSession(datetime.now()):
        rtkN = _overlayEtfDailyFromRtk(asof)
        if rtkN:
            logger.info("ETF_RTK overlay n=%d", rtkN)
    return ok


def _overlayEtfDailyFromRtk(asof) -> int:
    """仅五只 ETF 现货。stock_wind.rt_k 无期权，禁止拿来填 optionTicks。"""
    import pymysql

    cfg = dict(tushareDbConfig())
    cfg["database"] = os.environ.get("DB_NAME_WIND", "stock_wind")
    codes = list(OPT_UNDERLYING_QMT)
    try:
        conn = pymysql.connect(**cfg)
        try:
            with conn.cursor() as cur:
                marks = ",".join(["%s"] * len(codes))
                cur.execute(
                    "SELECT ts_code, pre_close, high, `open`, low, close, vol, trade_time "
                    "FROM rt_k WHERE ts_code IN (" + marks + ")",
                    codes,
                )
                rows = {}
                for rec in cur.fetchall() or []:
                    rows[str(rec[0])] = {
                        "ts_code": rec[0],
                        "pre_close": rec[1],
                        "high": rec[2],
                        "open": rec[3],
                        "low": rec[4],
                        "close": rec[5],
                        "vol": rec[6],
                        "trade_time": rec[7],
                    }
        finally:
            conn.close()
    except Exception:
        return 0
    n = 0
    with _cacheLock:
        for code in codes:
            if not etfBarLooksStub(etfDaily.get(code)):
                continue
            bar = etfDailyBarFromRtkRow(rows.get(code), asof)
            if bar is None:
                continue
            etfDaily[code] = bar
            n += 1
    return n


def _applyOptionTicks(payload, sampleCodes) -> None:
    if not payload:
        return
    nowMono = time.monotonic()
    wall = datetime.now()
    asof = wall.date()
    stamped = {}
    for code, tick in payload.items():
        if not isinstance(tick, dict):
            continue
        incoming = dict(tick)
        # 直连 QMT 的原始字段必须在生产端逐项标源；页面端不能靠缺省猜测。
        optionTickStampFieldSources(incoming, "qmt")
        with _cacheLock:
            existing = optionTicks.get(code)
        # 通达信盘口锁：只补量仓/最新，绝不整票换成 QMT 买卖档
        if (
            isinstance(existing, dict)
            and existing.get("fromTdx")
            and not incoming.get("fromTdx")
            and optionTickLooksUsable(existing, asof)
        ):
            patched = optionTickFillQtyFromIncoming(existing, incoming)
            if patched is not None:
                optionTickStampChangedFieldClocks(
                    patched, existing, wall.timestamp() * 1000.0
                )
                stamped[code] = patched
            continue
        if not optionTickShouldReplace(existing, incoming, asof):
            patched = optionTickFillQtyFromIncoming(existing, incoming)
            if patched is not None:
                optionTickStampChangedFieldClocks(
                    patched, existing, wall.timestamp() * 1000.0
                )
                stamped[code] = patched
            continue
        merged = optionTickMergeKeepQty(existing, incoming)
        optionTickStampChangedFieldClocks(
            merged, existing, wall.timestamp() * 1000.0
        )
        changedClock = any(
            merged.get(key) != (existing or {}).get(key)
            for key in OPT_TICK_FIELD_CHANGED_TIME_KEYS["last"]
            + OPT_TICK_FIELD_CHANGED_TIME_KEYS["qty"]
            + OPT_TICK_FIELD_CHANGED_TIME_KEYS["oi"]
            + OPT_TICK_FIELD_CHANGED_TIME_KEYS["book"]
        )
        if not optionTickPayloadChanged(existing, merged) and not changedClock:
            continue
        last = _asFloat(merged.get("lastPrice"))
        if last is None:
            last = _asFloat(merged.get("last"))
        hasTime = merged.get("time") not in (None, "", 0, 0.0) or merged.get(
            "stime"
        ) not in (None, "", 0, 0.0)
        if (not hasTime) and (
            (last is not None and last > 0) or tickHasFiveBand(merged)
        ):
            merged = dict(merged)
            merged["time"] = wall.timestamp() * 1000.0
        stamped[code] = merged
    if not stamped:
        return
    with _cacheLock:
        optionStats["lastPushMono"] = nowMono
        optionStats["lastWall"] = wall
        optionStats["okTicks"] += len(stamped)
        optionStats["uniqueCodes"].update(stamped.keys())
        optionTicks.update(stamped)
        for code in sampleCodes:
            tick = stamped.get(code)
            if tick is None:
                continue
            optionStats["sampleTicks"] += 1
            if tickHasFiveBand(tick):
                optionStats["sampleFiveBand"] += 1
    invalidateQuoteSnapshotJson()


def fetchOptionTicksLocked(xtdata, chunk, timeoutSec: float = OPT_RPC_TIMEOUT_SEC):
    gotLock = _acquireBackgroundRpcLock()
    if not gotLock:
        raise RuntimeError("whole-quote recovery has RPC priority")
    try:
        return xtdata.get_option_ticks(
            list(chunk), count=1, chunk_size=0, timeout_seconds=timeoutSec
        ) or {}
    finally:
        _rpcLock.release()


def loadOpenOptionQtyPriorityCodes(logger=None) -> frozenset:
    """读策略账本中的开放合约，失败时安全降级为空优先集。

    这是只读、短 TTL 查询；表不存在或 MySQL 暂断不应影响行情循环，更不能建表。
    """
    global _qtyPatchPositionCache
    now = time.monotonic()
    cachedAt, cachedCodes = _qtyPatchPositionCache
    if now - float(cachedAt) < OPT_QTY_POSITION_TTL_SEC:
        return cachedCodes
    try:
        import pymysql

        # 策略账本只是 P0 加权，不是行情主路径；MySQL 故障最多耗 2 秒，
        # 超时后沿用旧缓存，不能挤占下一轮 QMT RPC 的开始时刻。
        dbConfig = dict(tushareDbConfig())
        dbConfig["connect_timeout"] = min(
            int(dbConfig.get("connect_timeout") or OPT_QTY_POSITION_QUERY_TIMEOUT_SEC),
            OPT_QTY_POSITION_QUERY_TIMEOUT_SEC,
        )
        dbConfig["read_timeout"] = min(
            int(dbConfig.get("read_timeout") or OPT_QTY_POSITION_QUERY_TIMEOUT_SEC),
            OPT_QTY_POSITION_QUERY_TIMEOUT_SEC,
        )
        dbConfig["write_timeout"] = min(
            int(dbConfig.get("write_timeout") or OPT_QTY_POSITION_QUERY_TIMEOUT_SEC),
            OPT_QTY_POSITION_QUERY_TIMEOUT_SEC,
        )
        conn = pymysql.connect(**dbConfig)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts_code FROM opt_sp_position WHERE status='open'"
                )
                rows = cur.fetchall() or []
        finally:
            conn.close()
        codes = set()
        for row in rows:
            if isinstance(row, (tuple, list)):
                tsCode = row[0] if row else None
            elif isinstance(row, dict):
                tsCode = row.get("ts_code")
            else:
                tsCode = None
            try:
                codes.add(tsCodeToQmtOpt(str(tsCode)))
            except (TypeError, ValueError):
                continue
        _qtyPatchPositionCache = (now, frozenset(codes))
    except Exception as exc:
        if logger is not None:
            emitCooldownLog(
                logger,
                "opt_qty_open_position_read",
                logging.WARNING,
                "OPT_QTY open position priority unavailable: %s",
                exc,
                cooldownSec=60.0,
            )
        # 旧缓存仍比每轮清空更稳定；首次失败安全返回空集。
        if cachedCodes:
            _qtyPatchPositionCache = (now, cachedCodes)
        else:
            _qtyPatchPositionCache = (now, frozenset())
    return _qtyPatchPositionCache[1]


def optionQtyTdxUniverse(codes, metaMap) -> list:
    """仅选 TDX 主供两链的 OI 补丁宇宙，不动共享 optMeta。"""
    out = []
    seen = set()
    for raw in codes or []:
        code = str(raw).strip().upper()
        if not code.endswith((".SHO", ".SZO")) or code in seen:
            continue
        if isTdxServedOptionChain(code, (metaMap or {}).get(code)):
            out.append(code)
            seen.add(code)
    return out


def planShoMinuteOiBatch(
    codes,
    tickMap,
    metaMap,
    submittedAt,
    *,
    nowMono: float,
    chunkSize: int = OPT_SHO_MIN_OI_CHUNK,
    cooldownSec: float = OPT_SHO_MIN_OI_CODE_COOLDOWN_SEC,
    knownSlots: int = OPT_SHO_MIN_OI_KNOWN_SLOTS,
    probeSlots: int = OPT_SHO_MIN_OI_PROBE_SLOTS,
    eligibleCodes=None,
    unavailableUntil=None,
    refillKnownSlots: bool | None = None,
) -> list:
    """510050 分钟 OI：已验证合约为主，最多保留少量未知合约探测位。"""
    tickMap = tickMap or {}
    submittedAt = submittedAt or {}
    unavailableUntil = unavailableUntil or {}
    verified = None if eligibleCodes is None else set(eligibleCodes)
    known = []
    unknown = []
    for code in optionCodesForUndlSix(codes, metaMap, ("510050",)):
        lastSubmit = float(submittedAt.get(code) or 0.0)
        if lastSubmit > 0 and (float(nowMono) - lastSubmit) < float(cooldownSec):
            continue
        if float(unavailableUntil.get(code) or 0.0) > float(nowMono):
            continue
        rec = tickMap.get(code) or {}
        oiMs = optionTickFieldTimeMs(rec, "oi")
        vol = _asFloat(rec.get("vol"))
        if vol is None:
            vol = _asFloat(rec.get("volume"))
        row = (oiMs, lastSubmit, -float(vol or 0.0), code)
        if verified is None or code in verified:
            known.append(row)
        else:
            unknown.append(row)
    known.sort()
    unknown.sort()
    total = max(1, int(chunkSize))
    if verified is None:
        return [row[-1] for row in known[:total]]
    refill = OPT_SHO_MIN_OI_REFILL_ENABLE if refillKnownSlots is None else bool(
        refillKnownSlots
    )
    knownTake = min(total, max(0, int(knownSlots)))
    probeTake = min(total - knownTake, max(0, int(probeSlots)))
    selected = [row[-1] for row in known[:knownTake]] + [
        row[-1] for row in unknown[:probeTake]
    ]
    # 未知池为空/冷却时，把空闲探测槽返还给已验证合约；有未知候选时仍保持
    # 8+2 探索比例，且总量绝不超过既有 chunkSize。
    if refill and len(selected) < total:
        selected.extend(row[-1] for row in known[knownTake : knownTake + total - len(selected)])
    return selected


def szoMinuteOiLeaseDecision(
    record,
    *,
    nowMs: float,
    maxAgeSec: float = OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC,
    activeTtlSec: float = OPT_SZO_MIN_OI_ACTIVE_TTL_SEC,
    phaseWaitSec: float | None = None,
    graceSec: float = OPT_SZO_MIN_OI_OFFLOAD_GRACE_SEC,
    enabled: bool = True,
) -> dict:
    """解释单个 SZO 分钟 OI 是否仍可暂留在本地租约中。

    ``fresh`` 是严格年龄窗；``phase_grace`` 只允许跨过一个即将到来的下载
    相位，最长不超过 ``activeTtlSec``，且不会改变快照中的 ``oiTime``。所有
    无法确认来源、OI 或时间的记录均拒绝卸载，避免把缺失值当成新鲜值。
    """
    result = {
        "eligible": False,
        "reason": "disabled" if not enabled else "invalid_record",
        "ageSec": None,
        "oiTimeMs": 0.0,
        "source": "",
        "grace": False,
    }
    if not enabled or not isinstance(record, dict):
        return result
    source = str(record.get("oiSource") or "").strip().lower()
    result["source"] = source
    if source != "qmt_1m":
        result["reason"] = "source_not_qmt_1m"
        return result
    oi = _asFloat(
        record.get("oi")
        if record.get("oi") is not None
        else record.get("openInt")
    )
    if oi is None or oi <= 0:
        result["reason"] = "oi_missing"
        return result
    oiMs = optionTickFieldTimeMs(record, "oi")
    result["oiTimeMs"] = oiMs
    if oiMs <= 0:
        result["reason"] = "oi_time_missing"
        return result
    ageSec = (float(nowMs) - oiMs) / 1000.0
    result["ageSec"] = ageSec
    if ageSec < 0.0:
        result["reason"] = "future"
        return result
    strictAge = max(0.0, float(maxAgeSec))
    if ageSec <= strictAge:
        result.update({"eligible": True, "reason": "fresh"})
        return result
    # 只在下一相位即将到来时跨过短暂缺口，避免 45--60 秒之间既不刷新
    # 又不卸载；宽限不能超过真实活动 TTL，也不能超过显式宽限秒数。
    grace = max(0.0, min(60.0, float(graceSec)))
    ttl = max(strictAge, float(activeTtlSec))
    phaseWait = None if phaseWaitSec is None else max(0.0, float(phaseWaitSec))
    if grace > 0.0 and phaseWait is not None:
        if phaseWait <= grace and ageSec <= ttl:
            result.update({"eligible": True, "reason": "phase_grace", "grace": True})
            return result
    result["reason"] = "expired"
    return result


def szoMinuteOiOffloadDiagnostics(
    tickMap,
    metaMap,
    *,
    nowMs: float,
    maxAgeSec: float = OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC,
    enabled: bool | None = None,

) -> dict:
    """只读拆解 SZO 分钟 OI 卸载的每一道资格门控。

    ``eligible`` 与 ``eligible_n`` 仅在开关开启时产生；其它计数仍保留，便于
    区分「没有分钟来源」「OI/时间字段无效」「年龄超窗」和「确实可卸载但
    计划器没有使用」这几类情况。该函数不得修改 ``tickMap``/``metaMap``。
    """
    active = OPT_SZO_MIN_OI_OFFLOAD_ENABLE if enabled is None else bool(enabled)
    wallMs = float(nowMs)
    maxAgeMs = max(0.0, float(maxAgeSec)) * 1000.0
    phaseWaitSec = optionSzoLivePhaseWaitSec(
        datetime.fromtimestamp(wallMs / 1000.0)
    )
    statsOut = {
        "enabled": int(active),
        "target_n": 0,
        "record_invalid_n": 0,
        "source_qmt_1m_n": 0,
        "source_other_n": 0,
        "oi_positive_n": 0,
        "oi_nonpositive_n": 0,
        "oi_time_valid_n": 0,
        "oi_time_missing_n": 0,
        "age_future_n": 0,
        "age_old_n": 0,
        "age_window_n": 0,
        "lease_fresh_n": 0,
        "lease_grace_n": 0,
        "lease_expired_n": 0,
        "phase_wait_sec": phaseWaitSec,
        "eligible": set(),
        "eligible_n": 0,
    }
    for rawCode, rec in (tickMap or {}).items():
        code = str(rawCode).strip().upper()
        if (
            not code.endswith(".SZO")
            or undlSixFromMeta((metaMap or {}).get(code)) != "159915"
        ):
            continue
        statsOut["target_n"] += 1
        if not isinstance(rec, dict):
            statsOut["record_invalid_n"] += 1
            continue
        source = str(rec.get("oiSource") or "").strip().lower()
        if source == "qmt_1m":
            statsOut["source_qmt_1m_n"] += 1
        else:
            statsOut["source_other_n"] += 1
        oi = _asFloat(
            rec.get("oi")
            if rec.get("oi") is not None
            else rec.get("openInt")
        )
        if oi is not None and oi > 0:
            statsOut["oi_positive_n"] += 1
        else:
            statsOut["oi_nonpositive_n"] += 1
        oiMs = optionTickFieldTimeMs(rec, "oi")
        if oiMs <= 0:
            statsOut["oi_time_missing_n"] += 1
            continue
        statsOut["oi_time_valid_n"] += 1
        ageMs = wallMs - oiMs
        if ageMs < 0.0:
            statsOut["age_future_n"] += 1
        elif ageMs > maxAgeMs:
            statsOut["age_old_n"] += 1
        else:
            statsOut["age_window_n"] += 1
        lease = szoMinuteOiLeaseDecision(
            rec,
            nowMs=wallMs,
            maxAgeSec=maxAgeSec,
            activeTtlSec=OPT_SZO_MIN_OI_ACTIVE_TTL_SEC,
            phaseWaitSec=phaseWaitSec,
            graceSec=(
                OPT_SZO_MIN_OI_OFFLOAD_GRACE_SEC
                if OPT_SZO_MIN_OI_OFFLOAD_GRACE_ENABLE
                else 0.0
            ),
            enabled=active,
        )
        if lease["reason"] == "fresh":
            statsOut["lease_fresh_n"] += 1
        elif lease["reason"] == "phase_grace":
            statsOut["lease_grace_n"] += 1
        elif lease["reason"] == "expired":
            statsOut["lease_expired_n"] += 1
        if lease["eligible"]:
            statsOut["eligible"].add(code)
    statsOut["eligible_n"] = len(statsOut["eligible"])
    return statsOut


def szoMinuteOiOffloadCodes(
    tickMap,
    metaMap,
    *,
    nowMs: float,
    maxAgeSec: float = OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC,
    enabled: bool | None = None,
) -> set:
    """返回最近由 SZO 1m 成功观测 OI 的 159915 合约。

    这里只生成长尾排除集；P0 在计划器中先于长尾构建，因此持仓、ATM 或价量变化
    形成的紧急请求不会被卸载。OI 超龄或来源改变后自动退出本集合，重新进入轮转。
    """
    return set(
        szoMinuteOiOffloadDiagnostics(
            tickMap,
            metaMap,
            nowMs=nowMs,
            maxAgeSec=maxAgeSec,
            enabled=enabled,
        )["eligible"]
    )


def optionOiEffectiveSlaSnapshot(
    codes,
    tickMap,
    metaMap,
    *,
    nowMs: float,
    targetSec: float = OPT_EFFECTIVE_OI_SLA_SEC,
    enabled: bool | None = None,
) -> dict:
    """评估合并后 TDX 两链最终 OI，而非本轮 QMT RPC 的速度。"""
    active = OPT_EFFECTIVE_OI_SLA_ENABLE if enabled is None else bool(enabled)
    universe = optionQtyTdxUniverse(codes, metaMap)
    target = max(0.0, float(targetSec))

    def summarize(items) -> dict:
        ages = []
        staleN = 0
        for code in items:
            rec = (tickMap or {}).get(code) or {}
            oi = _asFloat(
                rec.get("oi")
                if rec.get("oi") is not None
                else rec.get("openInt")
            )
            oiMs = optionTickFieldTimeMs(rec, "oi")
            ageSec = (float(nowMs) - oiMs) / 1000.0 if oiMs > 0 else None
            valid = oi is not None and oi > 0 and ageSec is not None and ageSec >= 0.0
            if not valid:
                staleN += 1
                continue
            ages.append(float(ageSec))
            if ageSec > target:
                staleN += 1
        ages.sort()
        p95 = None
        if ages:
            p95 = ages[max(0, min(len(ages) - 1, math.ceil(len(ages) * 0.95) - 1))]
        return {
            "totalN": len(items),
            "validN": len(ages),
            "staleN": staleN,
            "p95AgeSec": round(p95, 1) if p95 is not None else None,
            "maxAgeSec": round(ages[-1], 1) if ages else None,
            "status": "OK" if items and staleN == 0 else "DEGRADED",
        }

    byUnderlying = {
        undl: summarize(
            [code for code in universe if undlSixFromMeta((metaMap or {}).get(code)) == undl]
        )
        for undl in OPT_QTY_TDX_UNDLS
    }
    summary = summarize(universe)
    summary["byUnderlying"] = byUnderlying
    if not active:
        summary["status"] = "DISABLED"
        for chain in byUnderlying.values():
            chain["status"] = "DISABLED"
    elif summary["totalN"] > 0 and summary["staleN"] == 0:
        summary["status"] = "OK"
    else:
        summary["status"] = "DEGRADED"
    return summary


def _optionQtyRankedCodes(codes, tickMap, field: str) -> list:
    def rank(code):
        rec = (tickMap or {}).get(code) or {}
        if field == "vol":
            value = _asFloat(rec.get("vol"))
            if value is None:
                value = _asFloat(rec.get("volume"))
        else:
            value = _asFloat(rec.get("oi"))
            if value is None:
                value = _asFloat(rec.get("openInt"))
            if value is None:
                value = _asFloat(rec.get("openInterest"))
        return (-(float(value) if value is not None else -1.0), code)

    return sorted(codes, key=rank)


def optionQtyPriorityCodes(
    universe,
    tickMap,
    metaMap,
    spotByUndl,
    *,
    heldCodes=None,
    urgentCodes=None,
    priorityPerUnderlying: int = OPT_QTY_PRIORITY_PER_UNDL,
    underlyings=OPT_QTY_TDX_UNDLS,
) -> list:
    """P0 顺序：开放持仓 → TDX 价/量变动未复核 OI → ATM C/P → 高成交 → 高持仓量。

    每条 TDX 链各自保底，避免 159915 的大成交合约吞掉 510050 的槽位。
    `priorityPerUnderlying` 是普通候选的每链预算，不是开放仓位的硬上限：
    仓位超过预算时必须留在 P0 队列并由 SLA 日志显式报容量不足，不能暗中落入长尾。
    """
    meta = metaMap or {}
    held = {str(x).strip().upper() for x in (heldCodes or ()) if str(x).strip()}
    urgent = [str(x).strip().upper() for x in (urgentCodes or ()) if str(x).strip()]
    perUndl = max(1, int(priorityPerUnderlying))
    byUndl = []
    for undl in underlyings or ():
        chain = [c for c in universe if undlSixFromMeta(meta.get(c)) == undl]
        if not chain:
            continue
        ordered = []
        heldInChain = sorted(c for c in chain if c in held)

        def add(items):
            for code in items:
                if code in chain and code not in ordered:
                    ordered.append(code)

        add(heldInChain)
        add(code for code in urgent if code in chain)
        spot = _asFloat((spotByUndl or {}).get(undl))
        strikes = sorted(
            {
                k
                for code in chain
                for k in [_asFloat((meta.get(code) or {}).get("k"))]
                if k is not None and k > 0
            },
            key=lambda k: (abs(k - spot) if spot is not None else float("inf"), k),
        )
        # P0 每链只有 3 个固定席位；只取最接近现货的一个行权价的 C/P，
        # 第三席让给最高成交/持仓，不能被第二个 ATM 邻档挤掉。
        for strike in strikes[:1]:
            add(
                sorted(
                    code
                    for code in chain
                    if _asFloat((meta.get(code) or {}).get("k")) == strike
                )
            )
        add(_optionQtyRankedCodes(chain, tickMap, "vol"))
        add(_optionQtyRankedCodes(chain, tickMap, "oi"))
        byUndl.append(ordered[: max(perUndl, len(heldInChain))])
    # P0 槽位按标的交织：保守档仅 4 席时也必须给 510050/159915 各留保底，
    # 不能因列表顺序让前一条链独占本轮。
    out = []
    maxLen = max((len(items) for items in byUndl), default=0)
    for i in range(maxLen):
        for items in byUndl:
            if i < len(items):
                out.append(items[i])
    return out


def optionQtyTdxDemandCodes(universe, tickMap, metaMap) -> list:
    """TDX 已见成交/价格变动、而 OI 尚未复核的合约，按两条链公平排入紧急队列。

    TDX 不提供 OI，不能写入或伪造该字段；但其最新价/成交量变化是低成本的
    "OI 可能已变化" 信号。仅当该变化时间严格晚于最后一次 QMT OI 观测时入队。
    """
    ticks = tickMap or {}
    meta = metaMap or {}
    byUndl = []
    for undl in OPT_QTY_TDX_UNDLS:
        candidates = []
        for code in universe or ():
            if undlSixFromMeta(meta.get(code)) != undl:
                continue
            rec = ticks.get(code) or {}
            oiObserved = optionTickFieldTimeMs(rec, "oi")
            changed = max(
                optionTickFieldChangedTimeMs(rec, "last"),
                optionTickFieldChangedTimeMs(rec, "qty"),
            )
            if changed > 0 and changed > oiObserved:
                # TDX 的持续成交会反复推进 changed；按最新变化倒序会使同一张
                # 热合约永久占住 P0。优先最久未被 QMT 复核 OI 的候选，未观测
                # 过 OI（0）排最前；同一 OI 年龄再按更早的变动时间服务。
                candidates.append((oiObserved if oiObserved > 0 else -1, changed, str(code).strip().upper()))
        byUndl.append([
            code
            for _oiObserved, _changed, code in sorted(
                candidates, key=lambda x: (x[0], x[1], x[2])
            )
        ])
    out = []
    maxLen = max((len(items) for items in byUndl), default=0)
    for index in range(maxLen):
        for items in byUndl:
            if index < len(items):
                out.append(items[index])
    return out


def optionQtyP0DiagnosticSnapshot(
    plan,
    demandCodes,
    qmtRepairCodes,
    heldCodes,
    tickMap,
    metaMap,
    spotByUndl,
    *,
    nowMs: float,
    pendingSinceMs: dict,
) -> dict:
    """只读拆解 P0 来源，并统计进程内连续待处理年龄。

    `demand` 的真实语义仅是 TDX last/qty 变动晚于最近 OI 观测；ATM、开放
    仓位和 QMT 修复属于最终 `priority_all` 的其它来源，不能混称 demand。
    `*_wait_*` 是本进程首次连续观察到该请求后的等待年龄，不冒充交易所事件
    时间；同一请求期间 last/qty 再变化不会重置年龄，退出集合后才清除。
    """
    ticks = tickMap or {}
    meta = metaMap or {}
    priority = [str(code).strip().upper() for code in (plan or {}).get("priority_all", ())]
    # `priority_all` 是候选集合；实际占用本轮 P0 槽位的是 `priority`。两者
    # 必须分开，否则长尾候选的旧 OI 会把“当前 P0 实际年龄”稀释掉。
    p0Codes = [str(code).strip().upper() for code in (plan or {}).get("priority", ())]
    demand = [str(code).strip().upper() for code in (demandCodes or ())]
    demandSet = set(demand)
    repairSet = {
        str(code).strip().upper() for code in (qmtRepairCodes or ()) if str(code).strip()
    }
    heldSet = {
        str(code).strip().upper() for code in (heldCodes or ()) if str(code).strip()
    }
    wallMs = float(nowMs)

    demandReason = {}
    demandMissingOi = 0
    demandByUndl = {undl: 0 for undl in OPT_QTY_TDX_UNDLS}
    for code in demand:
        rec = ticks.get(code) or {}
        oiObserved = optionTickFieldTimeMs(rec, "oi")
        if oiObserved <= 0:
            demandMissingOi += 1
        lastPending = optionTickFieldChangedTimeMs(rec, "last") > oiObserved
        qtyPending = optionTickFieldChangedTimeMs(rec, "qty") > oiObserved
        if lastPending and qtyPending:
            reason = "both"
        elif lastPending:
            reason = "last_only"
        elif qtyPending:
            reason = "qty_only"
        else:
            # 调用方若传入非当前 demand，必须显式暴露而非猜测原因。
            reason = "unknown"
        demandReason[code] = reason
        undl = undlSixFromMeta(meta.get(code))
        if undl in demandByUndl:
            demandByUndl[undl] += 1

    activePendingKeys = {
        *("demand:" + code for code in demandSet),
        *("repair:" + code for code in repairSet),
    }
    if isinstance(pendingSinceMs, dict):
        for key in list(pendingSinceMs):
            if key not in activePendingKeys:
                pendingSinceMs.pop(key, None)
        for key in activePendingKeys:
            pendingSinceMs.setdefault(key, wallMs)

    def waitStats(prefix: str, codes) -> tuple[float, float, float]:
        ages = sorted(
            max(0.0, (wallMs - float(pendingSinceMs.get(prefix + code, wallMs))) / 1000.0)
            for code in codes
        )
        if not ages:
            return 0.0, 0.0, 0.0
        p50 = ages[max(0, math.ceil(len(ages) * 0.50) - 1)]
        p95 = ages[max(0, math.ceil(len(ages) * 0.95) - 1)]
        return round(p50, 1), round(p95, 1), round(ages[-1], 1)

    atmCodes = set()
    for undl in OPT_QTY_TDX_UNDLS:
        spot = _asFloat((spotByUndl or {}).get(undl))
        chain = [code for code in priority if undlSixFromMeta(meta.get(code)) == undl]
        strikes = sorted(
            {
                strike
                for code in chain
                for strike in [_asFloat((meta.get(code) or {}).get("k"))]
                if strike is not None and strike > 0
            },
            key=lambda strike: (
                abs(strike - spot) if spot is not None else float("inf"), strike
            ),
        )
        if strikes:
            atmStrike = strikes[0]
            atmCodes.update(
                code
                for code in chain
                if _asFloat((meta.get(code) or {}).get("k")) == atmStrike
            )

    roles = {
        "held": 0,
        "demand_last_only": 0,
        "demand_qty_only": 0,
        "demand_both": 0,
        "demand_unknown": 0,
        "atm": 0,
        "activity_rank": 0,
        "qmt_repair": 0,
    }
    for code in priority:
        if code in repairSet:
            roles["qmt_repair"] += 1
        elif code in heldSet:
            roles["held"] += 1
        elif code in demandSet:
            roles["demand_" + demandReason.get(code, "unknown")] += 1
        elif code in atmCodes:
            roles["atm"] += 1
        else:
            # 计划器剩余静态位来自高成交/高持仓排序；同一合约可能同时属于两者，
            # 现有返回值没有保留精确选中阶段，因此如实合并命名。
            roles["activity_rank"] += 1

    demandP50, demandP95, demandMax = waitStats("demand:", demandSet)
    repairP50, repairP95, repairMax = waitStats("repair:", repairSet)

    oiAges = []
    oiMissing = 0
    for code in p0Codes:
        observedMs = optionTickFieldTimeMs(ticks.get(code) or {}, "oi")
        if observedMs <= 0:
            oiMissing += 1
            continue
        oiAges.append(max(0.0, (wallMs - float(observedMs)) / 1000.0))
    oiAges.sort()
    if oiAges:
        p0OiP50 = round(oiAges[max(0, math.ceil(len(oiAges) * 0.50) - 1)], 1)
        p0OiP95 = round(oiAges[max(0, math.ceil(len(oiAges) * 0.95) - 1)], 1)
        p0OiMax = round(oiAges[-1], 1)
    else:
        p0OiP50 = p0OiP95 = p0OiMax = None
    return {
        "demand": len(demandSet),
        "demand_last_only": sum(v == "last_only" for v in demandReason.values()),
        "demand_qty_only": sum(v == "qty_only" for v in demandReason.values()),
        "demand_both": sum(v == "both" for v in demandReason.values()),
        "demand_unknown": sum(v == "unknown" for v in demandReason.values()),
        "demand_missing_oi": demandMissingOi,
        "demand_by_underlying": demandByUndl,
        "demand_wait_p50_sec": demandP50,
        "demand_wait_p95_sec": demandP95,
        "demand_wait_max_sec": demandMax,
        "p0_total": len(priority),
        "p0_held": roles["held"],
        "p0_demand_last_only": roles["demand_last_only"],
        "p0_demand_qty_only": roles["demand_qty_only"],
        "p0_demand_both": roles["demand_both"],
        "p0_demand_unknown": roles["demand_unknown"],
        "p0_atm": roles["atm"],
        "p0_activity_rank": roles["activity_rank"],
        "qmt_repair": len(repairSet),
        "p0_qmt_repair": roles["qmt_repair"],
        "repair_wait_p50_sec": repairP50,
        "repair_wait_p95_sec": repairP95,
        "repair_wait_max_sec": repairMax,
        "p0_oi_age_p50_sec": p0OiP50,
        "p0_oi_age_p95_sec": p0OiP95,
        "p0_oi_age_max_sec": p0OiMax,
        "p0_oi_missing": oiMissing,
    }


def optionQtyQmtOiRepairCodes(
    codes,
    tickMap,
    metaMap,
    spotByUndl,
    *,
    heldCodes=None,
    nowMs=None,
    maxAgeSec: float = OPT_QTY_QMT_OI_REPAIR_STALE_SEC,
) -> list:
    """仅抽取 QMT 主导链中 OI 真缺失的关键合约，作为临时 P0 修复。

    subscribe_quote 是动态推送；一段时间没有回调仅代表该合约没有新事件，不能
    推断 OI 缺失，更不能挤占 510050/159915 的 P0。只有字段本身为缺失才修复。
    """
    stale = []
    meta = metaMap or {}
    ticks = tickMap or {}
    for raw in codes or ():
        code = str(raw).strip().upper()
        if not code.endswith((".SHO", ".SZO")):
            continue
        if undlSixFromMeta(meta.get(code)) not in OPT_QMT_TICK_UNDERLYINGS:
            continue
        rec = ticks.get(code) or {}
        oi = _asFloat(rec.get("oi"))
        if oi is None:
            oi = _asFloat(rec.get("openInt"))
        if oi is None:
            oi = _asFloat(rec.get("openInterest"))
        if oi is None:
            stale.append(code)
    return optionQtyPriorityCodes(
        stale,
        ticks,
        meta,
        spotByUndl,
        heldCodes=heldCodes,
        priorityPerUnderlying=OPT_QTY_QMT_REPAIR_PER_UNDL,
        underlyings=OPT_QMT_TICK_UNDERLYINGS,
    )


def _optionQtyTailOrderOneUnderlying(codes, tickMap, metaMap) -> list:
    """单条标的链：按到期月交织，月内优先处理最老 OI。

    OI 年龄排序只改变长尾的服务顺序，不改变 P0、批量大小或请求并发。
    没有字段级 ``oiTime`` 时保留旧的成交量/持仓量排序，避免把旧形状快照
    误判成可比较的年龄数据。
    """
    groups = {}
    for code in codes:
        rec = (tickMap or {}).get(code) or {}
        meta = (metaMap or {}).get(code) or {}
        rawExp = str(meta.get("exp") or meta.get("expiry") or "").strip()
        try:
            expiry = date.fromisoformat(rawExp[:10]).isoformat()
        except ValueError:
            expiry = "9999-12-31"
        oi = _asFloat(rec.get("oi"))
        if oi is None:
            oi = _asFloat(rec.get("openInt"))
        if oi is None:
            oi = _asFloat(rec.get("openInterest"))
        vol = _asFloat(rec.get("vol"))
        if vol is None:
            vol = _asFloat(rec.get("volume"))
        oiMs = optionTickFieldTimeMs(rec, "oi")
        oiAgeRank = 0 if oi is None or oiMs <= 0 else 1
        # ``oiMs`` 越小表示观测越早；缺失/无字段时间统一为0，仍由
        # 成交量和持仓量作为稳定的次级排序。开关关闭时精确回到旧顺序。
        ageKey = oiMs if oi is not None and oiMs > 0 else 0.0
        if OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE:
            sortKey = (
                oiAgeRank,
                ageKey,
                -(float(vol) if vol is not None else -1.0),
                -(float(oi) if oi is not None else -1.0),
                code,
            )
        else:
            sortKey = (
                0 if oi is None else 1,
                0.0,
                -(float(vol) if vol is not None else -1.0),
                -(float(oi) if oi is not None else -1.0),
                code,
            )
        # ``sortKey`` 已包含代码作为最终稳定键；不要再次追加代码，避免
        # 长尾排序元组出现重复字段并掩盖排序口径。
        groups.setdefault(expiry, []).append(sortKey)
    orderedGroups = [
        [item[-1] for item in sorted(groups[expiry])]
        for expiry in sorted(groups)
    ]
    out = []
    maxLen = max((len(items) for items in orderedGroups), default=0)
    for index in range(maxLen):
        for items in orderedGroups:
            if index < len(items):
                out.append(items[index])
    return out


def _optionQtyTailOrder(codes, tickMap, metaMap) -> list:
    """TDX 两链的长尾先各自按月排序，再按标的严格轮转。

    同一到期月内 159915 常有更高成交量；若把两条链先混入一个月桶，510050
    会被其完全挤出 probe10 的 4 个尾部席位。这里不增加请求数，只确保两条
    TDX 链在容量允许时都得到确定的轮转份额。
    """
    meta = metaMap or {}
    seen = set()
    groups = []
    for undl in OPT_QTY_TDX_UNDLS:
        chain = []
        for raw in codes or ():
            code = str(raw).strip().upper()
            if not code or code in seen or undlSixFromMeta(meta.get(code)) != undl:
                continue
            seen.add(code)
            chain.append(code)
        if chain:
            groups.append(_optionQtyTailOrderOneUnderlying(chain, tickMap, meta))
    rest = [
        str(raw).strip().upper()
        for raw in (codes or ())
        if str(raw).strip() and str(raw).strip().upper() not in seen
    ]
    if rest:
        groups.append(_optionQtyTailOrderOneUnderlying(rest, tickMap, meta))
    return interleaveLists(groups)


def optionQtyTailShareSnapshot(tailCodes, metaMap) -> dict:
    """统计本轮实际尾部名额按标的的分配，不参与计划器决策。"""
    counts = {undl: 0 for undl in OPT_QTY_TDX_UNDLS}
    other = 0
    meta = metaMap or {}
    for rawCode in tailCodes or ():
        code = str(rawCode).strip().upper()
        undl = undlSixFromMeta(meta.get(code))
        if undl in counts:
            counts[undl] += 1
        else:
            other += 1
    counts["other"] = other
    counts["total"] = sum(counts.values())
    return counts


def optionQtyTailAgeDiagnosticSnapshot(
    tailCodes,
    selectedCodes,
    tickMap,
    metaMap,
    *,
    nowMs: float,
    staleSec: float = OPT_QTY_P1_MAX_SEC,
) -> dict:
    """只读比较长尾全集与本轮入选项的 OI 年龄，供候选顺序取证。

    这里刻意不返回候选顺序、也不写任何游标：它的唯一职责是回答“当前最老
    的尾部 OI 是否正被本轮服务”。没有有效 ``oiTime`` 或未来时间戳会单列，
    不把它们伪造成陈旧数据。
    """
    selected = {
        str(code).strip().upper()
        for code in (selectedCodes or ())
        if str(code).strip()
    }
    wallMs = float(nowMs)
    threshold = max(0.0, float(staleSec))
    out = {
        undl: {
            "totalN": 0,
            "validN": 0,
            "missingTimeN": 0,
            "futureN": 0,
            "staleN": 0,
            "selectedN": 0,
            "selectedValidN": 0,
            "selectedStaleN": 0,
            "ageP50Sec": None,
            "ageP95Sec": None,
            "ageMaxSec": None,
            "selectedAgeP50Sec": None,
            "selectedAgeP95Sec": None,
            "selectedAgeMaxSec": None,
        }
        for undl in OPT_QTY_TDX_UNDLS
    }
    agesByUndl = {undl: [] for undl in OPT_QTY_TDX_UNDLS}
    selectedAgesByUndl = {undl: [] for undl in OPT_QTY_TDX_UNDLS}
    for rawCode in tailCodes or ():
        code = str(rawCode).strip().upper()
        undl = undlSixFromMeta((metaMap or {}).get(code))
        if undl not in out:
            continue
        item = out[undl]
        item["totalN"] += 1
        isSelected = code in selected
        if isSelected:
            item["selectedN"] += 1
        oiMs = optionTickFieldTimeMs((tickMap or {}).get(code) or {}, "oi")
        if oiMs <= 0.0:
            item["missingTimeN"] += 1
            continue
        ageSec = (wallMs - oiMs) / 1000.0
        if ageSec < 0.0:
            item["futureN"] += 1
            continue
        item["validN"] += 1
        agesByUndl[undl].append(ageSec)
        if ageSec > threshold:
            item["staleN"] += 1
        if isSelected:
            item["selectedValidN"] += 1
            selectedAgesByUndl[undl].append(ageSec)
            if ageSec > threshold:
                item["selectedStaleN"] += 1

    def applyAges(item, ages, prefix: str) -> None:
        if not ages:
            return
        ordered = sorted(ages)
        stem = prefix + "Age" if prefix else "age"
        item[stem + "P50Sec"] = round(
            ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)], 1
        )
        item[stem + "P95Sec"] = round(
            ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 1
        )
        item[stem + "MaxSec"] = round(ordered[-1], 1)

    for undl, item in out.items():
        applyAges(item, agesByUndl[undl], "")
        applyAges(item, selectedAgesByUndl[undl], "selected")
    return out


def _optionQtyTakeRound(items, start: int, size: int) -> tuple[list, int]:
    if not items or size <= 0:
        return [], 0
    n = len(items)
    offset = int(start) % n
    take = min(n, max(0, int(size)))
    return [items[(offset + i) % n] for i in range(take)], offset


def planOptionQtyPatchRound(
    codes,
    tickMap,
    metaMap,
    spotByUndl,
    *,
    rotate: int,
    roundN: int,
    batchSize: int,
    p0Slots: int | None = None,
    tailSlots: int | None = None,
    priorityPerUnderlying: int = OPT_QTY_PRIORITY_PER_UNDL,
    heldCodes=None,
    p0Rotate: int = 0,
    extraPriorityCodes=None,
    demandCodes=None,
    tailExcludeUnderlyings=(),
    tailExcludeCodes=(),
    gapSec: float | None = None,
    p0TargetSec: float = OPT_QTY_P0_TARGET_SEC,
    dynamicP0Loan: bool = False,
    p0LoanGuard: float = 1.0,
    p0LoanCap: int | None = None,
) -> dict:
    """纯计划器：可测试、无 RPC；返回 P0/尾部批次与各自游标。"""
    universe = optionQtyTdxUniverse(codes, metaMap)
    priorityAll = optionQtyPriorityCodes(
        universe,
        tickMap,
        metaMap,
        spotByUndl,
        heldCodes=heldCodes,
        urgentCodes=demandCodes,
        priorityPerUnderlying=priorityPerUnderlying,
    )
    for raw in extraPriorityCodes or ():
        code = str(raw).strip().upper()
        if code and code not in priorityAll:
            priorityAll.append(code)
        if code and code not in universe:
            universe.append(code)
    prioritySet = set(priorityAll)
    excludedTailUndls = {
        str(value).split(".", 1)[0]
        for value in (tailExcludeUnderlyings or ())
        if str(value).strip()
    }
    excludedTailCodes = {
        str(value).strip().upper()
        for value in (tailExcludeCodes or ())
        if str(value).strip()
    }
    tail = _optionQtyTailOrder(
        [
            code
            for code in universe
            if code not in prioritySet
            and code not in excludedTailCodes
            and undlSixFromMeta(metaMap.get(code)) not in excludedTailUndls
        ],
        tickMap,
        metaMap,
    )
    total = max(0, min(len(universe), int(batchSize)))
    if p0Slots is None:
        p0Slots = max(0, total // 2)
    if tailSlots is None:
        tailSlots = max(0, total - int(p0Slots))
    reservedP0 = min(max(0, int(p0Slots)), total)
    p0Need = reservedP0
    if gapSec is not None and float(p0TargetSec) > 0:
        guardedGapSec = float(gapSec) * (
            max(1.0, float(p0LoanGuard)) if dynamicP0Loan else 1.0
        )
        p0Need = optionQtySloSlots(
            len(priorityAll), guardedGapSec, float(p0TargetSec)
        )
    p0Budget = min(reservedP0, p0Need) if dynamicP0Loan else reservedP0
    if dynamicP0Loan and p0LoanCap is not None:
        # 借槽必须是硬上限：即使 SLO 公式认为可以多借，也至少保留
        # `reservedP0 - p0LoanCap` 个实际 P0。默认 cap=1，故 probe10
        # 最多从 6 个 P0 借出 1 个，不改变单批总量。
        boundedCap = max(0, min(reservedP0, int(p0LoanCap)))
        p0Budget = max(p0Budget, reservedP0 - boundedCap)
    p0Take = min(len(priorityAll), p0Budget, total)
    p0Loaned = max(0, reservedP0 - p0Take) if dynamicP0Loan else 0
    if p0LoanCap is not None:
        p0Loaned = min(p0Loaned, max(0, int(p0LoanCap)))
    tailBudget = max(0, int(tailSlots)) + p0Loaned
    tailTake = min(len(tail), tailBudget, total - p0Take)
    # P0 集合不足时，把剩余 RPC 容量还给长尾，而不是空转。
    if tailTake < total - p0Take:
        tailTake = min(len(tail), total - p0Take)
    p0Batch, p0Start = _optionQtyTakeRound(priorityAll, p0Rotate, p0Take)
    tailBatch, tailStart = _optionQtyTakeRound(tail, rotate, tailTake)
    batch = p0Batch + tailBatch
    return {
        "round": int(roundN),
        "universe": universe,
        "priority": p0Batch,
        "priority_all": priorityAll,
        "tail": tailBatch,
        "tail_all": tail,
        "batch": batch,
        "priority_round": bool(p0Batch),
        "p0_need": p0Need,
        "p0_loaned": p0Loaned,
        "p0_loan_cap": (
            max(0, int(p0LoanCap)) if p0LoanCap is not None else None
        ),
        "p0_start": p0Start,
        "tail_start": tailStart,
        "rotate_advance": len(tailBatch),
        "next_rotate": (tailStart + len(tailBatch)) % len(tail) if tail else 0,
        "next_p0_rotate": (p0Start + len(p0Batch)) % len(priorityAll)
        if priorityAll
        else 0,
    }


def optionQtyTailPlanImpactSnapshot(
    currentPlan,
    strictPlan,
    metaMap,
    *,
    gapSec: float,
) -> dict:
    """只读比较现行成功 TTL 与严格 OI 时间租约的长尾容量成本。

    该函数只读取两个已构造的计划，不重排、不改游标、不调用 RPC。严格计划中的
    ``returnedToQmtN`` 是原成功 TTL 卸载、但不满足真实 ``oiTime`` 租约而会回到
    QMT 尾部的合约数；``tailEtaSec`` 是按当前 start-to-start 间隔得到的理论周期，
    不是对实际字段 SLA 的承诺。
    """
    safeGapSec = max(0.0, float(gapSec))

    def summarize(plan) -> dict:
        tailAll = [str(code).strip().upper() for code in (plan or {}).get("tail_all") or ()]
        tail = [str(code).strip().upper() for code in (plan or {}).get("tail") or ()]
        tailTakeN = len(tail)
        tailAllN = len(tailAll)
        etaSec = (
            math.ceil(tailAllN / tailTakeN) * safeGapSec
            if tailAllN > 0 and tailTakeN > 0
            else 0.0
        )
        return {
            "tailAllN": tailAllN,
            "tailTakeN": tailTakeN,
            "tailEtaSec": round(etaSec, 1),
            "share": optionQtyTailShareSnapshot(tail, metaMap),
            "p0N": len((plan or {}).get("priority") or ()),
            "p0AllN": len((plan or {}).get("priority_all") or ()),
            "p0Need": int((plan or {}).get("p0_need") or 0),
            "p0Loaned": int((plan or {}).get("p0_loaned") or 0),
        }

    current = summarize(currentPlan)
    strict = summarize(strictPlan)
    currentTail = {
        str(code).strip().upper()
        for code in (currentPlan or {}).get("tail_all") or ()
        if str(code).strip()
    }
    strictTail = {
        str(code).strip().upper()
        for code in (strictPlan or {}).get("tail_all") or ()
        if str(code).strip()
    }
    p0Same = all(
        current[key] == strict[key]
        for key in ("p0N", "p0AllN", "p0Need", "p0Loaned")
    ) and list((currentPlan or {}).get("priority") or ()) == list(
        (strictPlan or {}).get("priority") or ()
    )
    return {
        "current": current,
        "strict": strict,
        "returnedToQmtN": len(strictTail - currentTail),
        "p0Same": p0Same,
    }


def _optionQtyServedPrefix(planned, servedCodes) -> int:
    served = {str(code).strip().upper() for code in (servedCodes or ())}
    n = 0
    for code in planned or []:
        if code not in served:
            break
        n += 1
    return n


def optionQtySloSlots(size: int, gapSec: float, targetSec: float) -> int:
    if int(size) <= 0:
        return 0
    return max(1, int(math.ceil(float(size) * max(0.0, gapSec) / targetSec)))


def shouldUseOptionQtyDynamicP0Loan(
    modeName: str,
    enabled: bool,
    *,
    demandN: int = 0,
    repairN: int = 0,
) -> bool:
    """兼容旧调用方的无年龄门控判定；生产 poll 使用有界决策函数。

    该函数保留是为了兼容已有单元测试/外部诊断脚本；它不再参与
    ``pollOptionQtyPatchRound``。新路径必须使用 ``optionQtyP0LoanDecision``，
    否则无法同时检查需求等待年龄和实际 P0 OI 年龄。

    `demandCodes` 会随 TDX 价量变化逐轮改变；若此时只按游标取部分 P0，
    新换入的紧急合约可能在集合重排中延后。因此任何价量紧急或主链修复需求
    都恢复原 6 张 P0，宁可暂时不改善长尾。
    """
    return (
        bool(enabled)
        and str(modeName).strip().lower() == "probe10"
        and int(demandN) <= 0
        and int(repairN) <= 0
    )


def optionQtyP0LoanDecision(
    modeName: str,
    enabled: bool,
    *,
    demandN: int = 0,
    demandWaitP95Sec: float | None = None,
    p0OiAgeP95Sec: float | None = None,
    p0OiAgeMaxSec: float | None = None,
    repairN: int = 0,
    maxLoanSlots: int = OPT_QTY_P0_LOAN_MAX_SLOTS,
    allowTwoSlots: bool = OPT_QTY_P0_LOAN_ALLOW_TWO,
    secondDemandMaxN: int = OPT_QTY_P0_LOAN_SECOND_MAX_DEMAND_N,
    secondWaitMaxSec: float = OPT_QTY_P0_LOAN_SECOND_MAX_WAIT_SEC,
    secondOiP95MaxSec: float = OPT_QTY_P0_LOAN_SECOND_MAX_OI_P95_SEC,
    secondOiMaxSec: float = OPT_QTY_P0_LOAN_SECOND_MAX_OI_MAX_SEC,
) -> dict:
    """根据连续等待年龄和实际 P0 OI 年龄决定最多借出几个槽位。

    这是调度前的纯函数安全门，不发 RPC，也不提高批量或并发。任何指标
    缺失、非有限或超阈值都返回 cap=0；只有 probe10 才允许借槽。第一槽
    默认最多一个，第二槽还需要显式开关和更严格的需求/年龄阈值。
    """
    reason = "bounded_disabled"
    if not bool(enabled):
        return {"cap": 0, "reason": reason}
    if str(modeName).strip().lower() != "probe10":
        return {"cap": 0, "reason": "mode_not_probe10"}
    if int(repairN) > 0:
        return {"cap": 0, "reason": "qmt_repair_pending"}

    try:
        demandCount = int(demandN)
        maxSlots = max(0, min(4, int(maxLoanSlots)))
    except (TypeError, ValueError):
        return {"cap": 0, "reason": "invalid_count"}
    if demandCount < 0 or maxSlots <= 0:
        return {"cap": 0, "reason": "invalid_count"}

    def finiteNonNegative(value) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) and number >= 0.0 else None

    waitP95 = finiteNonNegative(demandWaitP95Sec)
    oiP95 = finiteNonNegative(p0OiAgeP95Sec)
    oiMax = finiteNonNegative(p0OiAgeMaxSec)
    if waitP95 is None or oiP95 is None or oiMax is None:
        return {"cap": 0, "reason": "metric_missing"}
    if demandCount > int(OPT_QTY_P0_LOAN_MAX_DEMAND_N):
        return {"cap": 0, "reason": "demand_count_high"}
    if waitP95 > float(OPT_QTY_P0_LOAN_MAX_WAIT_SEC):
        return {"cap": 0, "reason": "demand_wait_high"}
    if oiP95 > float(OPT_QTY_P0_LOAN_MAX_OI_P95_SEC):
        return {"cap": 0, "reason": "p0_oi_p95_high"}
    if oiMax > float(OPT_QTY_P0_LOAN_MAX_OI_MAX_SEC):
        return {"cap": 0, "reason": "p0_oi_max_high"}

    # `maxLoanSlots` 是调用方本轮的有效上限；生产调用传入配置值，测试和
    # 离线评估可以显式传2来验证第二槽的严格门控。
    cap = maxSlots
    reason = "bounded_ok"
    if (
        cap >= 2
        and bool(allowTwoSlots)
        and demandCount <= int(secondDemandMaxN)
        and waitP95 <= float(secondWaitMaxSec)
        and oiP95 <= float(secondOiP95MaxSec)
        and oiMax <= float(secondOiMaxSec)
    ):
        cap = 2
        reason = "bounded_two_ok"
    else:
        cap = min(cap, 1)
    return {"cap": cap, "reason": reason}


def seedShoMinuteOiEligibleCodes(codes, metaMap, asof) -> set:
    """只读现有 SHO 1m DAT，预识别今天曾提供 OI 的合约，不触发 RPC。"""
    wanted = optionCodesForUndlSix(codes, metaMap, ("510050",))
    localBars = readQmtLocalBars(wanted, OPT_SZO_MIN_PERIOD, QMT_DAT_TAIL_MIN)
    observedAtMs = datetime.now().timestamp() * 1000.0
    return {
        code
        for code in wanted
        if (tick := szoMinuteRecordsToOiCandidate(
            localBars.get(code), asof, observedAtMs=observedAtMs
        ))
        and tick.get("openInt") is not None
        and tick.get("oiTime") is not None
    }


def applyLocalMinuteOiCandidates(codes, asof, *, now: datetime | None = None) -> dict:
    """直读本地 1m DAT，仅将严格更新的 OI 候选写入正式快照。"""
    wanted = [str(code).strip() for code in (codes or []) if str(code).strip()]
    localBars = readQmtLocalBars(wanted, OPT_SZO_MIN_PERIOD, QMT_DAT_TAIL_MIN)
    observedAtMs = (
        now.timestamp() * 1000.0
        if isinstance(now, datetime)
        else datetime.now().timestamp() * 1000.0
    )
    minuteTicks = {}
    for code in wanted:
        tick = szoMinuteRecordsToOiCandidate(
            localBars.get(code), asof, observedAtMs=observedAtMs
        )
        if tick and tick.get("openInt") is not None and tick.get("oiTime") is not None:
            minuteTicks[code] = tick
    if now is not None:
        nowMs = now.timestamp() * 1000.0
        minuteTicks = {
            code: tick
            for code, tick in minuteTicks.items()
            if -90.0
            <= (nowMs - float(tick["oiTime"])) / 1000.0
            <= OPT_SHO_MIN_OI_MAX_OBS_AGE_SEC
        }
    applied = 0
    with _cacheLock:
        for code, minuteTick in minuteTicks.items():
            overlaid = overlaySzoMinuteOiCandidate(optionTicks.get(code), minuteTick)
            if overlaid is None:
                continue
            optionTicks[code] = overlaid
            applied += 1
    if applied:
        invalidateQuoteSnapshotJson()
    return {
        "files": sum(1 for code in wanted if localBars.get(code)),
        "observed": len(minuteTicks),
        "observedCodes": sorted(minuteTicks),
        "applied": applied,
        "oldestOiMs": min(
            (float(tick["oiTime"]) for tick in minuteTicks.values()),
            default=0.0,
        ),
    }


def shoMinuteOiLeaseShadowSnapshot(
    tickMap,
    metaMap,
    *,
    nowMs: float,
    currentOffload=None,
    thresholds=OPT_SHO_MIN_OI_LEASE_SHADOW_THRESHOLDS,
) -> dict:
    """只读评估 510050 分钟 OI 的候选年龄租约，不参与调度。

    该诊断故意不调用 ``shoMinuteOiOffloadCodes``，也不改变任何全局集合；它只
    将已合并快照按来源、OI/时间字段和年龄窗口拆分，供后续 A/B 决定是否启用
    正式时间租约。``currentOffload`` 由调用方传入，用于比较现行成功 TTL 与
    候选年龄窗口的交集。
    """
    wallMs = float(nowMs)
    normalizedThresholdValues = set()
    for rawThreshold in thresholds or ():
        try:
            threshold = float(rawThreshold)
        except (TypeError, ValueError):
            continue
        if math.isfinite(threshold) and threshold > 0.0:
            normalizedThresholdValues.add(threshold)
    normalizedThresholds = tuple(sorted(normalizedThresholdValues))
    currentCodes = {
        str(code).strip().upper()
        for code in (currentOffload or ())
        if str(code).strip()
    }
    leaseCodes = {str(int(threshold)): set() for threshold in normalizedThresholds}
    statsOut = {
        "target_n": 0,
        "record_invalid_n": 0,
        "source_qmt_1m_n": 0,
        "source_other_n": 0,
        "oi_positive_n": 0,
        "oi_nonpositive_n": 0,
        "oi_time_valid_n": 0,
        "oi_time_missing_n": 0,
        "age_future_n": 0,
        "age_old_n": 0,
        "age_n": 0,
        "age_p50_sec": None,
        "age_p90_sec": None,
        "age_max_sec": None,
        "current_offload_n": len(currentCodes),
        "lease_n": {key: 0 for key in leaseCodes},
        "overlap_n": {key: 0 for key in leaseCodes},
        # 用不可变排序元组输出，供纯影子计划器复用；绝不写回任何卸载集合。
        "lease_codes": {key: () for key in leaseCodes},
    }
    ages = []
    maxThreshold = max(normalizedThresholds, default=0.0)
    for rawCode, rawRecord in (tickMap or {}).items():
        code = str(rawCode).strip().upper()
        if (
            not code.endswith(".SHO")
            or undlSixFromMeta((metaMap or {}).get(code) or (metaMap or {}).get(rawCode))
            != "510050"
        ):
            continue
        statsOut["target_n"] += 1
        if not isinstance(rawRecord, dict):
            statsOut["record_invalid_n"] += 1
            continue
        source = str(rawRecord.get("oiSource") or "").strip().lower()
        if source == "qmt_1m":
            statsOut["source_qmt_1m_n"] += 1
        else:
            statsOut["source_other_n"] += 1
        oi = _asFloat(
            rawRecord.get("oi")
            if rawRecord.get("oi") is not None
            else rawRecord.get("openInt")
        )
        if oi is None or oi <= 0.0:
            statsOut["oi_nonpositive_n"] += 1
            continue
        statsOut["oi_positive_n"] += 1
        oiMs = optionTickFieldTimeMs(rawRecord, "oi")
        if oiMs <= 0.0:
            statsOut["oi_time_missing_n"] += 1
            continue
        statsOut["oi_time_valid_n"] += 1
        ageSec = (wallMs - oiMs) / 1000.0
        if ageSec < 0.0:
            statsOut["age_future_n"] += 1
            continue
        if source != "qmt_1m":
            continue
        ages.append(ageSec)
        statsOut["age_n"] += 1
        if maxThreshold > 0.0 and ageSec > maxThreshold:
            statsOut["age_old_n"] += 1
        for threshold in normalizedThresholds:
            key = str(int(threshold))
            if ageSec <= threshold:
                leaseCodes[key].add(code)
    if ages:
        ages.sort()

        def nearestRank(q):
            index = max(0, min(len(ages) - 1, math.ceil(float(q) * len(ages)) - 1))
            return ages[index]

        statsOut["age_p50_sec"] = round(nearestRank(0.50), 1)
        statsOut["age_p90_sec"] = round(nearestRank(0.90), 1)
        statsOut["age_max_sec"] = round(ages[-1], 1)
    for key, codes in leaseCodes.items():
        statsOut["lease_n"][key] = len(codes)
        statsOut["overlap_n"][key] = len(codes & currentCodes)
        statsOut["lease_codes"][key] = tuple(sorted(codes))
    return statsOut


def maybeLogShoMinuteOiLeaseShadow(logger, snapshot: dict, *, nowMono: float) -> bool:
    """按低频节流输出 SHO OI 年龄影子统计；绝不改变调度状态。"""
    global _shoMinuteOiLeaseShadowLastLogMono
    if not OPT_SHO_MIN_OI_LEASE_SHADOW_ENABLE:
        return False
    mono = float(nowMono)
    if (
        _shoMinuteOiLeaseShadowLastLogMono > 0.0
        and mono - _shoMinuteOiLeaseShadowLastLogMono
        < OPT_SHO_MIN_OI_LEASE_SHADOW_REPORT_SEC
    ):
        return False
    _shoMinuteOiLeaseShadowLastLogMono = mono
    lease = snapshot.get("lease_n") or {}
    overlap = snapshot.get("overlap_n") or {}
    logger.info(
        "OPT_SHO_OI_LEASE_SHADOW target=%d source_qmt_1m=%d source_other=%d "
        "oi_pos=%d oi_time=%d future=%d old=%d age_n=%d age_p50_s=%s age_p90_s=%s "
        "age_max_s=%s current_offload=%d lease45=%d lease60=%d lease75=%d lease90=%d "
        "overlap45=%d overlap60=%d overlap75=%d overlap90=%d",
        int(snapshot.get("target_n") or 0),
        int(snapshot.get("source_qmt_1m_n") or 0),
        int(snapshot.get("source_other_n") or 0),
        int(snapshot.get("oi_positive_n") or 0),
        int(snapshot.get("oi_time_valid_n") or 0),
        int(snapshot.get("age_future_n") or 0),
        int(snapshot.get("age_old_n") or 0),
        int(snapshot.get("age_n") or 0),
        snapshot.get("age_p50_sec") if snapshot.get("age_p50_sec") is not None else "-",
        snapshot.get("age_p90_sec") if snapshot.get("age_p90_sec") is not None else "-",
        snapshot.get("age_max_sec") if snapshot.get("age_max_sec") is not None else "-",
        int(snapshot.get("current_offload_n") or 0),
        int(lease.get("45") or 0),
        int(lease.get("60") or 0),
        int(lease.get("75") or 0),
        int(lease.get("90") or 0),
        int(overlap.get("45") or 0),
        int(overlap.get("60") or 0),
        int(overlap.get("75") or 0),
        int(overlap.get("90") or 0),
    )
    return True


def pollShoMinuteOiRound(
    logger,
    xtdata,
    codes,
    asof,
    *,
    now: datetime | None = None,
    nowMono: float | None = None,
    uniqueN: int = 0,
    silenceSec: float = 0.0,
    quoteRecovery: bool = False,
) -> dict:
    """510050 长尾 OI 单轮：逐合约学习可用性；仅通道故障/已知集塌陷熔断。"""
    global _shoMinuteOiLastRoundMono, _shoMinuteOiLastSuccessMono
    global _shoMinuteOiCircuitUntilMono, _shoMinuteOiSubmitMono, _shoMinuteOiHealth
    global _shoMinuteOiEligibleCodes, _shoMinuteOiUnavailableUntilMono
    global _shoMinuteOiSuccessMono, _shoMinuteOiCollapseRounds, _shoMinuteOiSeedAsof
    global _shoMinuteOiLeaseShadowLastLogMono
    global _optChunkOiGateLastActionMono, _optChunkOiGateLastLogMono
    now = now or datetime.now()
    mono = time.monotonic() if nowMono is None else float(nowMono)
    enabled = shoMinuteOiRuntimeEnabled()
    base = {
        "enabled": enabled,
        "submitted": False,
        "healthy": False,
        "elapsedSec": 0.0,
        "files": 0,
        "observed": 0,
        "applied": 0,
        "planned": 0,
        "reason": "gated",
    }
    # 本地分钟文件探测不访问 RPC；即使运行时开关关闭，也先完成可用性建模，
    # 便于灰度前从 health 观察真实可用覆盖，而不是等启用后才盲探。
    with _cacheLock:
        tickMap = {code: dict(rec) for code, rec in optionTicks.items() if rec}
        metaMap = {code: dict(rec) for code, rec in optionMeta.items() if rec}
    universe = optionCodesForUndlSix(codes, metaMap, ("510050",))
    if universe and _shoMinuteOiSeedAsof != asof:
        _shoMinuteOiEligibleCodes.update(
            seedShoMinuteOiEligibleCodes(codes, metaMap, asof)
        )
        _shoMinuteOiSeedAsof = asof
        logger.info(
            "OPT_SHO_MIN_OI lazy_seed eligible=%d universe=%d",
            len(_shoMinuteOiEligibleCodes),
            len(universe),
        )
    base["universeN"] = len(universe)
    base["eligibleN"] = len(_shoMinuteOiEligibleCodes)
    if not shouldRunShoMinuteOiDownload(
        now=now,
        enabled=enabled,
        uniqueN=uniqueN,
        silenceSec=silenceSec,
        quoteHold=_quoteHold.is_set(),
        rpcBusy=_optRpcBusy.is_set(),
        quoteRecovery=quoteRecovery,
        circuitUntilMono=_shoMinuteOiCircuitUntilMono,
        lastStartMono=_shoMinuteOiLastRoundMono,
        nowMono=mono,
    ):
        # “未到下一轮/运行时关闭”不是上一轮失败。保留最近一次实际请求的
        # healthy/reason/耗时，只刷新当前可用性规模，避免监控在两轮间误报红灯。
        health = dict(_shoMinuteOiHealth)
        health.update(
            {
                "enabled": enabled,
                "universeN": len(universe),
                "eligibleN": len(_shoMinuteOiEligibleCodes),
            }
        )
        _shoMinuteOiHealth = health
        return base
    eligibleBefore = set(_shoMinuteOiEligibleCodes)
    batch = planShoMinuteOiBatch(
        codes,
        tickMap,
        metaMap,
        _shoMinuteOiSubmitMono,
        nowMono=mono,
        eligibleCodes=eligibleBefore,
        unavailableUntil=_shoMinuteOiUnavailableUntilMono,
    )
    if not batch:
        base["reason"] = "cooldown"
        return base
    _shoMinuteOiLastRoundMono = mono
    started = time.perf_counter()
    submittedN = _downloadOptionPeriodStore(
        logger,
        xtdata,
        batch,
        asof.strftime("%Y%m%d"),
        OPT_SZO_MIN_PERIOD,
        OPT_SHO_MIN_OI_RPC_TIMEOUT_SEC,
    )
    elapsed = time.perf_counter() - started
    for code in batch:
        _shoMinuteOiSubmitMono[code] = mono
    local = applyLocalMinuteOiCandidates(batch, asof, now=now)
    observedCodes = set(local["observedCodes"])
    for code in batch:
        if code in observedCodes:
            _shoMinuteOiEligibleCodes.add(code)
            _shoMinuteOiUnavailableUntilMono.pop(code, None)
            _shoMinuteOiSuccessMono[code] = mono
        else:
            _shoMinuteOiEligibleCodes.discard(code)
            _shoMinuteOiSuccessMono.pop(code, None)
            _shoMinuteOiUnavailableUntilMono[code] = (
                mono + OPT_SHO_MIN_OI_UNAVAILABLE_COOLDOWN_SEC
            )
    if OPT_SHO_MIN_OI_LEASE_SHADOW_ENABLE:
        with _cacheLock:
            shadowTickMap = {
                code: dict(optionTicks.get(code) or {}) for code in universe
            }
        shadowSnapshot = shoMinuteOiLeaseShadowSnapshot(
            shadowTickMap,
            metaMap,
            nowMs=now.timestamp() * 1000.0,
            currentOffload=shoMinuteOiOffloadCodes(mono),
        )
        maybeLogShoMinuteOiLeaseShadow(
            logger, shadowSnapshot, nowMono=mono
        )
    observationAgeSec = (
        max(0.0, now.timestamp() - (float(local["oldestOiMs"]) / 1000.0))
        if local["oldestOiMs"] > 0
        else float("inf")
    )
    transportHealthy = shoMinuteOiRoundHealthy(
        submittedN=submittedN,
        plannedN=len(batch),
        elapsedSec=elapsed,
        maxSubmitSec=OPT_SHO_MIN_OI_MAX_SUBMIT_SEC,
        fileN=local["files"],
        observedN=local["observed"],
        observationAgeSec=observationAgeSec,
    )
    eligiblePlanned = set(batch) & eligibleBefore
    eligibleObserved = eligiblePlanned & observedCodes
    shouldCircuit, collapseRounds, decisionReason = shoMinuteOiCircuitDecision(
        submittedN=submittedN,
        plannedN=len(batch),
        elapsedSec=elapsed,
        maxSubmitSec=OPT_SHO_MIN_OI_MAX_SUBMIT_SEC,
        eligiblePlannedN=len(eligiblePlanned),
        eligibleObservedN=len(eligibleObserved),
        previousCollapseRounds=_shoMinuteOiCollapseRounds,
    )
    _shoMinuteOiCollapseRounds = collapseRounds
    if observedCodes:
        _shoMinuteOiLastSuccessMono = mono
    if shouldCircuit:
        _shoMinuteOiCircuitUntilMono = mono + OPT_SHO_MIN_OI_CIRCUIT_SEC
        reason = decisionReason
        emitCooldownLog(
            logger,
            "sho_minute_oi_circuit",
            logging.WARNING,
            "OPT_SHO_MIN_OI CIRCUIT planned=%d submitted=%d elapsed_s=%.2f "
            "files=%d observed=%d known=%d/%d obs_age_s=%.1f reason=%s rollback_s=%.0f",
            len(batch),
            submittedN,
            elapsed,
            local["files"],
            local["observed"],
            len(eligibleObserved),
            len(eligiblePlanned),
            observationAgeSec,
            decisionReason,
            OPT_SHO_MIN_OI_CIRCUIT_SEC,
        )
    elif local["observed"] == len(batch):
        reason = "ok"
    else:
        reason = decisionReason
    healthy = transportHealthy and not shouldCircuit
    result = {
        "enabled": enabled,
        "submitted": True,
        "healthy": healthy,
        "elapsedSec": round(elapsed, 3),
        "files": local["files"],
        "observed": local["observed"],
        "applied": local["applied"],
        "observationAgeSec": round(observationAgeSec, 1),
        "planned": len(batch),
        "reason": reason,
        "universeN": len(universe),
        "eligibleN": len(_shoMinuteOiEligibleCodes),
        "unavailableN": sum(
            1
            for until in _shoMinuteOiUnavailableUntilMono.values()
            if float(until or 0.0) > mono
        ),
        "offloadN": len(shoMinuteOiOffloadCodes(mono)),
        "eligibleObserved": len(eligibleObserved),
        "eligiblePlanned": len(eligiblePlanned),
        "first": batch[0],
        "at": now.isoformat(timespec="seconds"),
    }
    _shoMinuteOiHealth = result
    invalidateQuoteSnapshotJson()
    logger.info(
        "OPT_SHO_MIN_OI planned=%d submitted=%d files=%d observed=%d applied=%d "
        "elapsed_s=%.2f obs_age_s=%.1f healthy=%d reason=%s eligible=%d unavailable=%d "
        "offload=%d first=%s",
        len(batch),
        submittedN,
        local["files"],
        local["observed"],
        local["applied"],
        elapsed,
        observationAgeSec,
        int(healthy),
        reason,
        result["eligibleN"],
        result["unavailableN"],
        result["offloadN"],
        batch[0],
    )
    return result


def optionQtyRoundHealthy(
    *,
    servedN: int,
    plannedN: int,
    elapsedSec: float,
    maxRpcSec: float,
    declaredHealthy: bool,
    quoteHold: bool,
    rpcBusy: bool,
) -> bool:
    """只有 RPC 与已声明的 OI SLA 都健康时，才允许放大下一轮。"""
    return bool(
        declaredHealthy
        and int(servedN) == int(plannedN)
        and float(elapsedSec) <= float(maxRpcSec)
        and not quoteHold
        and not rpcBusy
    )


def optionQtyRoundProbeSafe(
    *,
    servedN: int,
    plannedN: int,
    elapsedSec: float,
    maxRpcSec: float,
    p0Capacity: bool,
    quoteHold: bool,
    rpcBusy: bool,
) -> bool:
    """单请求梯度探测的硬安全门槛；P1 未达 SLA 只标降级，不阻断取样。"""
    return bool(
        p0Capacity
        and int(servedN) == int(plannedN)
        and float(elapsedSec) <= float(maxRpcSec)
        and not quoteHold
        and not rpcBusy
    )


def pollOptionQtyPatchRound(
    logger,
    xtdata,
    codes,
    *,
    timeoutSec: float = 18.0,
) -> int:
    """分层补 TDX 两链的 vol/oi，串行调用 QMT 且真实报告 SLA/压力状态。"""
    global _qtyPatchRotate, _qtyPatchP0Rotate, _qtyPatchTailRotate
    global _qtyPatchMode, _qtyPatchGoodRounds, _qtyPatchLastStartMono, _qtyPatchRoundN
    global _qtyP0DiagLastLogMono, _qtyTailDiagLastLogMono
    global _szoOiOffloadDiagLastLogMono
    global _shoMinuteOiLeasePlanShadowLastLogMono
    if xtdata is None:
        return 0
    startMono = time.monotonic()
    gapSec = (
        startMono - _qtyPatchLastStartMono
        if _qtyPatchLastStartMono > 0
        else OPT_QTY_P0_TARGET_SEC
    )
    _qtyPatchLastStartMono = startMono
    with _cacheLock:
        tickMap = {code: dict(rec) for code, rec in optionTicks.items() if rec}
        metaMap = {code: dict(rec) for code, rec in optionMeta.items() if rec}
        spotByUndl = etfSpotByUndlSix(dict(etfTicks), dict(etfDaily))
    heldCodes = loadOpenOptionQtyPriorityCodes(logger)
    qmtRepairP0 = optionQtyQmtOiRepairCodes(
        codes,
        tickMap,
        metaMap,
        spotByUndl,
        heldCodes=heldCodes,
    )
    demandP0 = optionQtyTdxDemandCodes(codes, tickMap, metaMap)
    shoOffload = shoMinuteOiOffloadCodes(startMono)
    szoOffloadDiag = szoMinuteOiOffloadDiagnostics(
        tickMap,
        metaMap,
        nowMs=datetime.now().timestamp() * 1000.0,
        maxAgeSec=OPT_SZO_MIN_OI_OFFLOAD_MAX_AGE_SEC,
    )
    szoOffload = set(szoOffloadDiag["eligible"])
    if OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC > 0:
        if (
            startMono - _szoOiOffloadDiagLastLogMono
            >= OPT_SZO_OI_OFFLOAD_DIAG_LOG_SEC
        ):
            logger.info(
                "OPT_SZO_OI_DIAG enabled=%d target=%d invalid=%d "
                "source_qmt_1m=%d source_other=%d oi_pos=%d oi_bad=%d "
                "oi_time=%d oi_no_time=%d age_future=%d age_old=%d "
                "age_window=%d lease_fresh=%d lease_grace=%d lease_expired=%d "
                "phase_wait_s=%.1f eligible=%d",
                szoOffloadDiag["enabled"],
                szoOffloadDiag["target_n"],
                szoOffloadDiag["record_invalid_n"],
                szoOffloadDiag["source_qmt_1m_n"],
                szoOffloadDiag["source_other_n"],
                szoOffloadDiag["oi_positive_n"],
                szoOffloadDiag["oi_nonpositive_n"],
                szoOffloadDiag["oi_time_valid_n"],
                szoOffloadDiag["oi_time_missing_n"],
                szoOffloadDiag["age_future_n"],
                szoOffloadDiag["age_old_n"],
                szoOffloadDiag["age_window_n"],
                szoOffloadDiag["lease_fresh_n"],
                szoOffloadDiag["lease_grace_n"],
                szoOffloadDiag["lease_expired_n"],
                float(szoOffloadDiag["phase_wait_sec"])
                if szoOffloadDiag["phase_wait_sec"] is not None
                else -1.0,
                szoOffloadDiag["eligible_n"],
            )
            _szoOiOffloadDiagLastLogMono = startMono
    spec = OPT_QTY_MODE_SPECS[max(0, min(_qtyPatchMode, len(OPT_QTY_MODE_SPECS) - 1))]
    nowMs = datetime.now().timestamp() * 1000.0
    # 先用固定 P0 计划计算安全指标，再决定本轮最多能借几个槽。这样借槽
    # 不会把自身的降配结果当作“P0 年龄健康”，也不会因诊断开关关闭而失去
    # 连续等待时钟。
    metricsPlan = planOptionQtyPatchRound(
        codes,
        tickMap,
        metaMap,
        spotByUndl,
        rotate=_qtyPatchTailRotate,
        p0Rotate=_qtyPatchP0Rotate,
        roundN=_qtyPatchRoundN,
        batchSize=spec["chunkSize"] * spec["maxChunks"],
        p0Slots=spec["p0Slots"],
        tailSlots=spec["tailSlots"],
        priorityPerUnderlying=OPT_QTY_PRIORITY_PER_UNDL,
        heldCodes=heldCodes,
        extraPriorityCodes=qmtRepairP0,
        demandCodes=demandP0,
        tailExcludeCodes=shoOffload | szoOffload,
        dynamicP0Loan=False,
    )
    loanDiag = optionQtyP0DiagnosticSnapshot(
        metricsPlan,
        demandP0,
        qmtRepairP0,
        heldCodes,
        tickMap,
        metaMap,
        spotByUndl,
        nowMs=nowMs,
        pendingSinceMs=_qtyP0DiagPendingSinceMs,
    )
    loanDecision = optionQtyP0LoanDecision(
        spec["name"],
        bool(OPT_QTY_DYNAMIC_P0_LOAN_ENABLE and OPT_QTY_BOUNDED_P0_LOAN_ENABLE),
        demandN=loanDiag["demand"],
        demandWaitP95Sec=loanDiag["demand_wait_p95_sec"],
        p0OiAgeP95Sec=loanDiag["p0_oi_age_p95_sec"],
        p0OiAgeMaxSec=loanDiag["p0_oi_age_max_sec"],
        repairN=loanDiag["qmt_repair"],
        maxLoanSlots=OPT_QTY_P0_LOAN_MAX_SLOTS,
        allowTwoSlots=OPT_QTY_P0_LOAN_ALLOW_TWO,
    )
    boundedLoanCap = int(loanDecision["cap"])
    plan = planOptionQtyPatchRound(
        codes,
        tickMap,
        metaMap,
        spotByUndl,
        rotate=_qtyPatchTailRotate,
        p0Rotate=_qtyPatchP0Rotate,
        roundN=_qtyPatchRoundN,
        batchSize=spec["chunkSize"] * spec["maxChunks"],
        p0Slots=spec["p0Slots"],
        tailSlots=spec["tailSlots"],
        priorityPerUnderlying=OPT_QTY_PRIORITY_PER_UNDL,
        heldCodes=heldCodes,
        extraPriorityCodes=qmtRepairP0,
        demandCodes=demandP0,
        tailExcludeCodes=shoOffload | szoOffload,
        gapSec=gapSec,
        p0TargetSec=OPT_QTY_P0_TARGET_SEC,
        dynamicP0Loan=boundedLoanCap > 0,
        p0LoanGuard=OPT_QTY_P0_LOAN_GUARD,
        p0LoanCap=boundedLoanCap,
    )
    # 真实 OI 时间租约的候选只构造影子计划，绝不替换 ``plan``、游标、请求批次
    # 或卸载集合。先量化返还 QMT 的尾部成本，防止“修一条过龄分钟 OI，却拖慢整链”。
    if (
        OPT_SHO_MIN_OI_LEASE_SHADOW_ENABLE
        and OPT_SHO_MIN_OI_LEASE_PLAN_SHADOW_ENABLE
        and (
            _shoMinuteOiLeasePlanShadowLastLogMono <= 0.0
            or startMono - _shoMinuteOiLeasePlanShadowLastLogMono
            >= OPT_SHO_MIN_OI_LEASE_PLAN_SHADOW_REPORT_SEC
        )
    ):
        shadowStarted = time.perf_counter()
        leaseSnapshot = shoMinuteOiLeaseShadowSnapshot(
            tickMap,
            metaMap,
            nowMs=nowMs,
            currentOffload=shoOffload,
        )
        leaseCodes = leaseSnapshot.get("lease_codes") or {}
        strictImpacts = {}
        for threshold in OPT_SHO_MIN_OI_LEASE_SHADOW_THRESHOLDS:
            key = str(int(threshold))
            strictPlan = planOptionQtyPatchRound(
                codes,
                tickMap,
                metaMap,
                spotByUndl,
                rotate=_qtyPatchTailRotate,
                p0Rotate=_qtyPatchP0Rotate,
                roundN=_qtyPatchRoundN,
                batchSize=spec["chunkSize"] * spec["maxChunks"],
                p0Slots=spec["p0Slots"],
                tailSlots=spec["tailSlots"],
                priorityPerUnderlying=OPT_QTY_PRIORITY_PER_UNDL,
                heldCodes=heldCodes,
                extraPriorityCodes=qmtRepairP0,
                demandCodes=demandP0,
                tailExcludeCodes=set(leaseCodes.get(key) or ()) | szoOffload,
                gapSec=gapSec,
                p0TargetSec=OPT_QTY_P0_TARGET_SEC,
                dynamicP0Loan=boundedLoanCap > 0,
                p0LoanGuard=OPT_QTY_P0_LOAN_GUARD,
                p0LoanCap=boundedLoanCap,
            )
            strictImpacts[key] = optionQtyTailPlanImpactSnapshot(
                plan, strictPlan, metaMap, gapSec=gapSec,
            )
        currentImpact = optionQtyTailPlanImpactSnapshot(
            plan, plan, metaMap, gapSec=gapSec,
        )["current"]
        parts = []
        for threshold in OPT_SHO_MIN_OI_LEASE_SHADOW_THRESHOLDS:
            key = str(int(threshold))
            impact = strictImpacts[key]
            strict = impact["strict"]
            parts.append(
                "t%s=lease:%d return:%d tail:%d/%d eta:%.1f p0same:%d"
                % (
                    key,
                    int((leaseSnapshot.get("lease_n") or {}).get(key) or 0),
                    impact["returnedToQmtN"],
                    strict["tailAllN"],
                    strict["tailTakeN"],
                    strict["tailEtaSec"],
                    int(impact["p0Same"]),
                )
            )
        logger.info(
            "OPT_SHO_OI_LEASE_PLAN_SHADOW current_offload=%d current_tail=%d/%d "
            "current_eta_est_s=%.1f %s calc_ms=%.1f",
            int(leaseSnapshot.get("current_offload_n") or 0),
            currentImpact["tailAllN"],
            currentImpact["tailTakeN"],
            currentImpact["tailEtaSec"],
            " ".join(parts),
            (time.perf_counter() - shadowStarted) * 1000.0,
        )
        _shoMinuteOiLeasePlanShadowLastLogMono = startMono
    # 门控诊断始终旁路计算；即使日志关闭，也要让后续 OPT_CHUNK 让行门拿到
    # 当前需求等待/P0 OI 年龄，不能因日志开关改变调度安全性。
    diag = optionQtyP0DiagnosticSnapshot(
        plan,
        demandP0,
        qmtRepairP0,
        heldCodes,
        tickMap,
        metaMap,
        spotByUndl,
        nowMs=nowMs,
        pendingSinceMs=_qtyP0DiagPendingSinceMs,
    )
    updateOptionQtyPressureSnapshot(diag, gapSec=gapSec, nowMono=startMono)
    if OPT_QTY_P0_DIAG_LOG_SEC > 0:
        if (startMono - _qtyP0DiagLastLogMono) >= OPT_QTY_P0_DIAG_LOG_SEC:
            logger.info(
                "OPT_QTY_DIAG demand=%d missing_oi=%d last_only=%d qty_only=%d "
                "both=%d unknown=%d wait=%.1f/%.1f/%.1fs "
                "chain=510050:%d,159915:%d p0=%d held=%d "
                "p0_demand=%d/%d/%d/%d atm=%d activity_rank=%d "
                "qmt_repair=%d p0_repair=%d repair_wait=%.1f/%.1f/%.1fs "
                "p0_oi=%.1f/%.1f/%s missing=%d loan_cap=%d loan_reason=%s",
                diag["demand"],
                diag["demand_missing_oi"],
                diag["demand_last_only"],
                diag["demand_qty_only"],
                diag["demand_both"],
                diag["demand_unknown"],
                diag["demand_wait_p50_sec"],
                diag["demand_wait_p95_sec"],
                diag["demand_wait_max_sec"],
                diag["demand_by_underlying"].get("510050", 0),
                diag["demand_by_underlying"].get("159915", 0),
                diag["p0_total"],
                diag["p0_held"],
                diag["p0_demand_last_only"],
                diag["p0_demand_qty_only"],
                diag["p0_demand_both"],
                diag["p0_demand_unknown"],
                diag["p0_atm"],
                diag["p0_activity_rank"],
                diag["qmt_repair"],
                diag["p0_qmt_repair"],
                diag["repair_wait_p50_sec"],
                diag["repair_wait_p95_sec"],
                diag["repair_wait_max_sec"],
                diag["p0_oi_age_p50_sec"]
                if diag["p0_oi_age_p50_sec"] is not None
                else -1.0,
                diag["p0_oi_age_p95_sec"]
                if diag["p0_oi_age_p95_sec"] is not None
                else -1.0,
                f"{diag['p0_oi_age_max_sec']:.1f}"
                if diag["p0_oi_age_max_sec"] is not None
                else "-",
                diag["p0_oi_missing"],
                boundedLoanCap,
                loanDecision["reason"],
            )
            _qtyP0DiagLastLogMono = startMono
    _qtyPatchRoundN += 1
    if not plan["batch"]:
        return 0
    p0Need = int(plan["p0_need"])
    tailNeed = optionQtySloSlots(
        len(plan["tail_all"]), gapSec, OPT_QTY_P1_TARGET_SEC
    )
    # 不能满足时明确 DEGRADED；不偷加 QMT 并发/批量。
    p0Capacity = len(plan["priority"]) >= p0Need
    declaredHealthy = p0Capacity and len(plan["tail"]) >= tailNeed
    served = set()
    ok = 0
    failed = False
    parts = [
        plan["batch"][i : i + int(spec["chunkSize"])]
        for i in range(0, len(plan["batch"]), int(spec["chunkSize"]))
    ]
    for part in parts:
        if _quoteHold.is_set() or _optRpcBusy.is_set():
            failed = True
            break
        try:
            ticks = fetchOptionTicksLocked(
                xtdata, part, timeoutSec=min(float(timeoutSec), float(spec["maxRpcSec"]))
            )
        except Exception as exc:
            emitCooldownLog(
                logger,
                "opt_qty_rpc",
                logging.WARNING,
                "OPT_QTY fail n=%d first=%s %s",
                len(part),
                part[0],
                exc,
            )
            failed = True
            break
        if not ticks:
            failed = True
            break
        served.update(str(code).strip().upper() for code in ticks)
        _applyOptionTicks(ticks, [])
        ok += len(ticks)
    p0Advanced = _optionQtyServedPrefix(plan["priority"], served)
    tailAdvanced = _optionQtyServedPrefix(plan["tail"], served)
    if plan["priority_all"] and p0Advanced:
        _qtyPatchP0Rotate = (
            int(plan["p0_start"]) + p0Advanced
        ) % len(plan["priority_all"])
    if plan["tail_all"] and tailAdvanced:
        _qtyPatchTailRotate = (
            int(plan["tail_start"]) + tailAdvanced
        ) % len(plan["tail_all"])
        _qtyPatchRotate = _qtyPatchTailRotate
    elapsed = time.monotonic() - startMono
    probeSafe = (
        not failed
        and optionQtyRoundProbeSafe(
            servedN=len(served), plannedN=len(plan["batch"]), elapsedSec=elapsed,
            maxRpcSec=float(spec["maxRpcSec"]), p0Capacity=p0Capacity,
            quoteHold=_quoteHold.is_set(), rpcBusy=_optRpcBusy.is_set(),
        )
    )
    modeReason = "steady"
    if not probeSafe:
        if _qtyPatchMode > 0:
            _qtyPatchMode -= 1
            modeReason = "pressure_down"
        else:
            modeReason = "pressure_hold"
        _qtyPatchGoodRounds = 0
    else:
        _qtyPatchGoodRounds += 1
        if (
            _qtyPatchGoodRounds >= max(1, OPT_QTY_PROMOTE_GOOD_ROUNDS)
            and _qtyPatchMode < len(OPT_QTY_MODE_SPECS) - 1
        ):
            _qtyPatchMode += 1
            _qtyPatchGoodRounds = 0
            modeReason = "probe_up"
        elif not declaredHealthy:
            modeReason = "p1_degraded_hold"
    tailEta = (
        math.ceil(len(plan["tail_all"]) / max(1, len(plan["tail"]))) * gapSec
        if plan["tail_all"] and plan["tail"]
        else 0.0
    )
    sloReasons = []
    if not p0Capacity:
        sloReasons.append("p0_capacity")
    if len(plan["tail"]) < tailNeed:
        sloReasons.append("p1_target_capacity")
    if tailEta > OPT_QTY_P1_MAX_SEC:
        sloReasons.append("p1_max_eta")
    if not probeSafe:
        sloReasons.append("rpc_pressure")
    rawSlo = "OK" if declaredHealthy and probeSafe and not sloReasons else "DEGRADED"
    with _cacheLock:
        effectiveTickMap = {
            code: dict(optionTicks.get(code) or {})
            for code in optionQtyTdxUniverse(codes, metaMap)
        }
    effectiveSla = optionOiEffectiveSlaSnapshot(
        codes,
        effectiveTickMap,
        metaMap,
        nowMs=datetime.now().timestamp() * 1000.0,
    )
    tailShare = optionQtyTailShareSnapshot(plan["tail"], metaMap)
    if OPT_QTY_TAIL_DIAG_LOG_SEC > 0 and (
        startMono - _qtyTailDiagLastLogMono >= OPT_QTY_TAIL_DIAG_LOG_SEC
    ):
        tailDiag = optionQtyTailAgeDiagnosticSnapshot(
            plan["tail_all"], plan["tail"], tickMap, metaMap,
            nowMs=datetime.now().timestamp() * 1000.0,
        )
        shoTail = tailDiag["510050"]
        szoTail = tailDiag["159915"]
        logger.info(
            "OPT_QTY_TAIL_DIAG stale_s=%.0f "
            "all_510050=%d/%d/%d age_p95/max=%s/%s "
            "pick_510050=%d/%d/%d age_p95/max=%s/%s "
            "all_159915=%d/%d/%d age_p95/max=%s/%s "
            "pick_159915=%d/%d/%d age_p95/max=%s/%s",
            OPT_QTY_P1_MAX_SEC,
            shoTail["totalN"], shoTail["validN"], shoTail["staleN"],
            shoTail["ageP95Sec"] if shoTail["ageP95Sec"] is not None else "-",
            shoTail["ageMaxSec"] if shoTail["ageMaxSec"] is not None else "-",
            shoTail["selectedN"], shoTail["selectedValidN"], shoTail["selectedStaleN"],
            shoTail["selectedAgeP95Sec"]
            if shoTail["selectedAgeP95Sec"] is not None else "-",
            shoTail["selectedAgeMaxSec"]
            if shoTail["selectedAgeMaxSec"] is not None else "-",
            szoTail["totalN"], szoTail["validN"], szoTail["staleN"],
            szoTail["ageP95Sec"] if szoTail["ageP95Sec"] is not None else "-",
            szoTail["ageMaxSec"] if szoTail["ageMaxSec"] is not None else "-",
            szoTail["selectedN"], szoTail["selectedValidN"], szoTail["selectedStaleN"],
            szoTail["selectedAgeP95Sec"]
            if szoTail["selectedAgeP95Sec"] is not None else "-",
            szoTail["selectedAgeMaxSec"]
            if szoTail["selectedAgeMaxSec"] is not None else "-",
        )
        _qtyTailDiagLastLogMono = startMono
    logger.info(
        "OPT_QTY mode=%s next=%s tail_age=%d n=%d demand_p0=%d qmt_repair_p0=%d "
        "offload_sho=%d offload_szo=%d p0=%d/%d need=%d loan=%d guard=%.2f "
        "loan_cap=%d loan_reason=%s "
        "tail=%d/%d need=%d share=510050:%d,159915:%d,other:%d "
        "served=%d/%d p0rot=%d/%d tailrot=%d/%d gap=%.2fs rpc=%.2fs eta=%.1fs "
        "raw_slo=%s raw_reason=%s effective_slo=%s effective=%d/%d "
        "effective_p95=%s effective_max=%s effective_stale=%d "
        "eff_510050=%s:%d/%d:p95=%s eff_159915=%s:%d/%d:p95=%s mode_reason=%s",
        spec["name"],
        OPT_QTY_MODE_SPECS[_qtyPatchMode]["name"],
        int(OPT_QTY_TAIL_OI_AGE_PRIORITY_ENABLE),
        len(plan["universe"]),
        len(demandP0),
        len(qmtRepairP0),
        len(shoOffload),
        len(szoOffload),
        len(plan["priority"]),
        len(plan["priority_all"]),
        p0Need,
        int(plan["p0_loaned"]),
        OPT_QTY_P0_LOAN_GUARD,
        boundedLoanCap,
        loanDecision["reason"],
        len(plan["tail"]),
        len(plan["tail_all"]),
        tailNeed,
        tailShare["510050"],
        tailShare["159915"],
        tailShare["other"],
        ok,
        len(plan["batch"]),
        _qtyPatchP0Rotate,
        len(plan["priority_all"]),
        _qtyPatchTailRotate,
        len(plan["tail_all"]),
        gapSec,
        elapsed,
        tailEta,
        rawSlo,
        "+".join(sloReasons) if sloReasons else "ok",
        effectiveSla["status"],
        effectiveSla["validN"],
        effectiveSla["totalN"],
        effectiveSla["p95AgeSec"] if effectiveSla["p95AgeSec"] is not None else "-",
        effectiveSla["maxAgeSec"] if effectiveSla["maxAgeSec"] is not None else "-",
        effectiveSla["staleN"],
        effectiveSla["byUnderlying"]["510050"]["status"],
        effectiveSla["byUnderlying"]["510050"]["validN"],
        effectiveSla["byUnderlying"]["510050"]["totalN"],
        effectiveSla["byUnderlying"]["510050"]["p95AgeSec"]
        if effectiveSla["byUnderlying"]["510050"]["p95AgeSec"] is not None
        else "-",
        effectiveSla["byUnderlying"]["159915"]["status"],
        effectiveSla["byUnderlying"]["159915"]["validN"],
        effectiveSla["byUnderlying"]["159915"]["totalN"],
        effectiveSla["byUnderlying"]["159915"]["p95AgeSec"]
        if effectiveSla["byUnderlying"]["159915"]["p95AgeSec"] is not None
        else "-",
        modeReason,
    )
    return ok


def _pollOneOptionChunk(
    logger, xtdata, chunk, sampleCodes, depth: int = 0,
    timeoutSec: float | None = None, splitMin: int | None = None,
    splitMaxDepth: int | None = None,
):
    """单片 tick；超时且片够大则对半再拉，避免整片全丢。"""
    t0 = time.perf_counter()
    to = OPT_RPC_TIMEOUT_SEC if timeoutSec is None else float(timeoutSec)
    minN = OPT_CHUNK_SPLIT_MIN if splitMin is None else int(splitMin)
    maxDepth = (
        OPT_CHUNK_SPLIT_MAX_DEPTH if splitMaxDepth is None else int(splitMaxDepth)
    )
    try:
        ticks = fetchOptionTicksLocked(xtdata, chunk, timeoutSec=to)
        ms = (time.perf_counter() - t0) * 1000.0
        if ticks:
            _applyOptionTicks(ticks, sampleCodes)
        return len(ticks or {}), 0, ms
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000.0
        first = chunk[0] if chunk else ""
        if depth < maxDepth and len(chunk) >= minN:
            emitCooldownLog(
                logger, "OPT_FAIL", logging.WARNING,
                "OPT_CHUNK split n=%d first=%s depth=%d %s",
                len(chunk), first, depth, exc,
            )
            mid = len(chunk) // 2
            ok1, f1, m1 = _pollOneOptionChunk(
                logger, xtdata, chunk[:mid], sampleCodes, depth + 1,
                timeoutSec=to, splitMin=minN, splitMaxDepth=maxDepth,
            )
            ok2, f2, m2 = _pollOneOptionChunk(
                logger, xtdata, chunk[mid:], sampleCodes, depth + 1,
                timeoutSec=to, splitMin=minN, splitMaxDepth=maxDepth,
            )
            return ok1 + ok2, f1 + f2, max(ms, m1, m2)
        emitCooldownLog(
            logger, "OPT_FAIL", logging.WARNING,
            "OPT_CHUNK fail n=%d first=%s %s", len(chunk), first, exc,
        )
        return 0, 1, ms


def optionPollSlices(codes, metaMap=None, rotate: int = 0) -> list:
    """按标的分组后再打包到 OPT_CHUNK_SIZE；无要素时 rest 整组再切。"""
    groups = rotateList(groupOptionCodesByUnderlying(codes, metaMap), rotate)
    return packOptionPollSlices(groups, OPT_CHUNK_SIZE)


def _pollOptionRound(
    logger, xtdata, codes, sampleCodes, metaMap=None, rotate: int = 0,
    maxChunks=None, failAbort=None, chunkSize=None, yieldSec=None,
    timeoutSec=None,
):
    """交错名单按片 RPC；片间放锁并让出。连续失败则中止本轮，避免 8s×N 堵死。"""
    ok = 0
    fail = 0
    maxMs = 0.0
    abortN = OPT_POLL_FAIL_ABORT if failAbort is None else int(failAbort)
    size = OPT_CHUNK_SIZE if chunkSize is None else max(1, int(chunkSize))
    pause = OPT_CHUNK_YIELD_SEC if yieldSec is None else max(0.0, float(yieldSec))
    to = OPT_RPC_TIMEOUT_SEC if timeoutSec is None else float(timeoutSec)
    # 已按标的交错的名单直接切片；再按链打包会被 maxChunks 切成只打 50ETF。
    chunks = chunkCodes(list(codes or []), size)
    if maxChunks is not None:
        chunks = chunks[: max(0, int(maxChunks))]
    consecFail = 0
    for i, chunk in enumerate(chunks):
        if not running:
            break
        partOk, partFail, chunkMs = _pollOneOptionChunk(
            logger, xtdata, chunk, sampleCodes, 0, timeoutSec=to,
        )
        ok += partOk
        fail += partFail
        if chunkMs > maxMs:
            maxMs = chunkMs
        if partFail:
            consecFail += 1
            if abortN > 0 and consecFail >= abortN:
                emitCooldownLog(
                    logger, "OPT_FAIL", logging.WARNING,
                    "OPT_CHUNK abort consec_fail=%d done=%d remain=%d",
                    consecFail,
                    i + 1,
                    len(chunks) - i - 1,
                )
                fail += len(chunks) - i - 1
                break
        else:
            consecFail = 0
        if running and i + 1 < len(chunks):
            _sleepInterruptible(pause)
    return ok, fail, maxMs


def _tryRefreshOptionChain(logger, xtdata, oldCodes):
    """重拉 QMT 链并保留已有 TDX 两链；不达下限则保留旧列表。

    QMT 拉链接口只返回 510300/510500/588000。启动时 TDX 五链已经并入
    ``oldCodes``，日切重拉不能再用三链结果覆盖掉 510050/159915。
    """
    try:
        newCodes, per = loadEtfOptionCodesFromQmt(xtdata)
    except Exception as exc:
        logger.warning("OPT_CHAIN refresh fail %s", exc)
        return None
    if not optionChainMeetsFloor(per):
        logger.warning(
            "OPT_CHAIN refresh floor skip total=%d %s",
            len(newCodes or []),
            ",".join("%s=%d" % (k, v) for k, v in (per or {}).items()),
        )
        return None
    oldTdxCodes = optionQtyTdxUniverse(oldCodes, _copyOptionMeta())
    mergedCodes = mergeOptionUniverseCodes(newCodes, oldTdxCodes)
    if len(mergedCodes) != len(newCodes):
        logger.info(
            "OPT_CHAIN keep_tdx n=%d qmt_n=%d tdx_n=%d",
            len(mergedCodes),
            len(newCodes),
            len(oldTdxCodes),
        )
    added, dropped = optionChainDelta(oldCodes, mergedCodes)
    if added or dropped:
        pruneOptionCaches(mergedCodes)
        rememberOptionChainAdds(added, date.today())
        logger.info(
            "OPT_CHAIN refresh n=%d +%d -%d chunks=%d %s",
            len(mergedCodes),
            len(added),
            len(dropped),
            len(chunkCodes(mergedCodes, OPT_CHUNK_SIZE)),
            ",".join("%s=%d" % (k, v) for k, v in per.items()),
        )
    _seedListedTodayFromOptBasic(logger, date.today())
    reloadAdjustedOptionCodes(logger)
    if added:
        _fillOptionMetaGaps(logger, xtdata, len(added), preferCodes=added)
    return list(mergedCodes)


def _fillOptionMetaGaps(logger, xtdata, limit: int, preferCodes=None) -> int:
    """串行补链上缺失的 optMeta；limit 控制本轮张数。prefer 供拉链新增插到队首。"""
    n = max(0, int(limit or 0))
    if n <= 0 or not running:
        return 0
    nowMono = time.monotonic()
    with _cacheLock:
        codes = list(_optionChainCodes)
        have = set(optionMeta)
        triedAt = dict(_optionMetaTriedAt)
    need = optionMetaPreferCodes(
        preferCodes,
        optionMetaNeedCodes(
            codes,
            have,
            optionListedTodayCodes(date.today()),
            triedAt,
            nowMono,
            OPT_META_RETRY_SEC,
        ),
        have,
    )
    if not need:
        return 0
    filled = 0
    for code in need[:n]:
        if not running:
            break
        gotLock = _acquireBackgroundRpcLock()
        if not gotLock:
            break
        try:
            try:
                detail = xtdata.get_option_detail_data(code)
            finally:
                _rpcLock.release()
        except Exception as exc:
            logger.warning("OPT_META fail %s %s", code, exc)
            detail = None
        if not isinstance(detail, dict) or not detail:
            with _cacheLock:
                _optionMetaTriedAt[code] = time.monotonic()
            _sleepInterruptible(OPT_META_YIELD_SEC)
            continue
        meta = compactOptionMeta(code, detail)
        with _cacheLock:
            if meta:
                optionMeta[code] = meta
                filled += 1
                _optionMetaTriedAt.pop(code, None)
            else:
                _optionMetaTriedAt[code] = time.monotonic()
        _sleepInterruptible(OPT_META_YIELD_SEC)
    if filled:
        logger.info(
            "OPT_META filled=%d remain=%d",
            filled,
            max(0, len(need) - filled),
        )
    return filled


def _runOptionMetaFillLoop(logger, xtdata) -> None:
    """后台补合约要素。直播窗内停；缺口由轮询线程窄口补，避免挡 1d。"""
    logger.info("期权合约要素填充启动")
    while running:
        if not shouldFillOptionMeta(datetime.now()):
            _sleepInterruptible(1.0)
            continue
        filled = _fillOptionMetaGaps(logger, xtdata, OPT_META_BATCH)
        if not filled:
            _sleepInterruptible(5.0)


def _runOptionPollLoop(logger, xtdata, codes) -> None:
    global _lastEtfDailyMono, _lastOptDailyMono, _lastSzoDailyDlMono, _lastListedDailyDlMono
    global _listedDailyDlCursor
    global _szoSubOwned, _szoSubConfirmAsof, _szoSubUnavailable, _szoSubRetryMono
    global _szoSubLastSyncMono, _optHealthDeadPushMono
    global _lastDailyGateMode
    global _optionTickRefreshMono, _szoLiveDlMono, _szoLiveRoundMono, _szoLiveRoundPhase
    global _shoMinuteOiLastRoundMono, _shoMinuteOiLastSuccessMono
    global _shoMinuteOiCircuitUntilMono, _shoMinuteOiSubmitMono, _shoMinuteOiHealth
    global _shoMinuteOiEligibleCodes, _shoMinuteOiUnavailableUntilMono
    global _shoMinuteOiSuccessMono, _shoMinuteOiCollapseRounds, _shoMinuteOiSeedAsof
    global _shoMinuteOiLeaseShadowLastLogMono, _shoMinuteOiLeasePlanShadowLastLogMono
    codes = list(codes or [])
    _setOptionChainCodes(codes)
    # 直播窗现货走全推；仅五只都没有当日 last 时才 30s 日K 兜底。期权首轮立刻刷 1d。
    _lastEtfDailyMono = 0.0
    _lastOptDailyMono = 0.0
    _lastSzoDailyDlMono = 0.0
    _lastListedDailyDlMono = 0.0
    _listedDailyDlCursor = 0
    _szoSubOwned = set()
    _szoSubConfirmAsof = None
    _szoSubLastSyncMono = 0.0
    _szoSubUnavailable = False
    _szoSubRetryMono = 0.0
    _lastDailyGateMode = None
    _optionTickRefreshMono = {}
    _szoLiveDlMono = {}
    _szoLiveRoundMono = 0.0
    _szoLiveRoundPhase = ""
    _shoMinuteOiLastRoundMono = 0.0
    _shoMinuteOiLastSuccessMono = 0.0
    _shoMinuteOiCircuitUntilMono = 0.0
    _shoMinuteOiSubmitMono = {}
    _shoMinuteOiEligibleCodes = set()
    _shoMinuteOiUnavailableUntilMono = {}
    _shoMinuteOiSuccessMono = {}
    _shoMinuteOiCollapseRounds = 0
    _shoMinuteOiSeedAsof = None
    _shoMinuteOiLeaseShadowLastLogMono = 0.0
    _shoMinuteOiLeasePlanShadowLastLogMono = 0.0
    _optChunkOiGateLastActionMono = 0.0
    _optChunkOiGateLastLogMono = 0.0
    _shoMinuteOiHealth = {
        "enabled": bool(OPT_SHO_MIN_OI_ENABLE),
        "submitted": False,
        "healthy": False,
        "elapsedSec": None,
        "files": 0,
        "observed": 0,
        "applied": 0,
        "reason": "not_started",
    }
    lastChainAsof = None
    zipReadyMono = time.monotonic() + OPT_CHAIN_ZIP_DELAY_SEC
    chainZipFailN = 0
    chainFailUntil = 0.0
    lastZipMono = 0.0
    chainComplete = False
    sameSizeHits = 0
    seedChainN = len(codes)
    sampleCodes = tuple(codes[:3])
    metaMap = _copyOptionMeta()
    shoUniverseN = len(optionCodesForUndlSix(codes, metaMap, ("510050",)))
    if shoUniverseN:
        _shoMinuteOiEligibleCodes = seedShoMinuteOiEligibleCodes(
            codes, metaMap, date.today()
        )
        _shoMinuteOiSeedAsof = date.today()
    _shoMinuteOiHealth["universeN"] = shoUniverseN
    _shoMinuteOiHealth["eligibleN"] = len(_shoMinuteOiEligibleCodes)
    logger.info(
        "OPT_SHO_MIN_OI seed eligible=%d universe=%d runtime_enabled=%d",
        len(_shoMinuteOiEligibleCodes),
        shoUniverseN,
        int(shoMinuteOiRuntimeEnabled()),
    )
    szoMinFailUntil = {}
    quoteCacheEmptyHits = 0
    # 推送退化连续计数（2026-09-11）：命中率过低时累加，用于触发 force 重订
    subDegradeStreak = 0
    hadQuoteCacheHits = False
    lastCachePollMono = 0.0
    lastOptionQtyEndMono = 0.0
    quoteCacheRecheckPending = False
    lastForceMono = 0.0
    # force 重订只允许一次确认窗；确认失败后进入回退态，禁止无效的 90 秒 churn。
    optPushForceConfirmMono = 0.0
    optPushFallback = False
    optPushFallbackSinceMono = 0.0
    optPushRecoverySinceMono = 0.0
    lastSubCachedN = 0
    lastOptYieldLog = 0.0
    quotePollOnly = set()
    quotePollOnlyAsof = None
    healFailKey = ()
    healFailN = 0
    logger.info(
        "期权轮询启动 n=%d sho=%d szo=%d chunk=%d yield_s=%.2f zip_delay_s=%.1f sample=%s",
        len(codes),
        len(optionShoCodes(codes)),
        len(optionSzoCodes(codes)),
        OPT_CHUNK_SIZE,
        OPT_CHUNK_YIELD_SEC,
        OPT_CHAIN_ZIP_DELAY_SEC,
        ",".join(sampleCodes),
    )
    _seedListedTodayFromOptBasic(logger, date.today())
    while running:
        while _quoteHold.is_set() and running:
            _sleepInterruptible(0.2)
        if not running:
            break
        now = datetime.now()
        heavyWindow = isOptionHeavyRpcWindow(now)
        preopenBootstrap = isOptionPreopenBootstrapWindow(now)
        asof = now.date()
        t0 = time.perf_counter()
        nowMono = time.monotonic()
        inAuction = isOptionContinuousAuction(now)
        uniqueN = wholeQuoteUniqueN()
        silenceSec = wholeQuoteSilenceSec(now, nowMono)
        yieldQuote = shouldDeferOptionHeavyRpc(
            uniqueN,
            heavyWindow,
            silenceSec=silenceSec,
            quoteHold=_quoteHold.is_set(),
            pingBusy=isOptRpcBusy(),
        )
        if lastChainAsof is not None and asof != lastChainAsof:
            chainComplete = False
            sameSizeHits = 0
            lastZipMono = 0.0
            seedChainN = len(codes)
            lastChainAsof = None
            szoMinFailUntil = {}
        # TDX 合并不占 QMT RPC：即便 OPT_YIELD 也要刷盘口，否则量仓/买卖会冻住。
        metaMap = _copyOptionMeta()
        if shouldMergeTdxInOptionPoll():
            tdxN = pollTdxMergeOnce(logger)
            if tdxN:
                logger.info("TDX_OPT inline merge n=%d", tdxN)
        minuteOiRound = pollShoMinuteOiRound(
            logger,
            xtdata,
            codes,
            asof,
            now=now,
            nowMono=nowMono,
            uniqueN=uniqueN,
            silenceSec=silenceSec,
            quoteRecovery=bool(optPushForceConfirmMono and not optPushFallback),
        )
        minuteOiSubmitted = bool(minuteOiRound.get("submitted"))
        # OPT_QTY 是重型 QMT 量仓补丁。全推假活时若仍发 get_market_data_ex，
        # 会继续占用同一策略线程；因此 TDX 盘口照常合并，但 OI 等订阅恢复后再补。
        allowQty = shouldRunOptionQtyPatch(
            heavyWindow,
            bool(codes),
            quoteHold=_quoteHold.is_set(),
            yieldQuote=yieldQuote,
            quoteRecovery=bool(optPushForceConfirmMono and not optPushFallback),
            optionCallbackEmpty=bool(
                optPushFallback
                or optPushForceConfirmMono
                or quoteCacheEmptyHits > 0
                or quoteCacheRecheckPending
            ),
            optionCallbackFallback=bool(optPushFallback),
        )
        if allowQty and not minuteOiSubmitted:
            try:
                pollOptionQtyPatchRound(logger, xtdata, codes)
            except Exception as exc:
                logger.warning("OPT_QTY round %s", exc)
            finally:
                lastOptionQtyEndMono = time.monotonic()
        if yieldQuote:
            if (nowMono - lastOptYieldLog) >= OPT_YIELD_LOG_SEC:
                logger.info(
                    "OPT_YIELD whole_quote unique=%d silence_s=%.1f hold=%s busy=%s",
                    uniqueN,
                    silenceSec,
                    int(_quoteHold.is_set()),
                    int(isOptRpcBusy()),
                )
                lastOptYieldLog = nowMono
            _sleepInterruptible(OPT_YIELD_REST_SEC)
            continue
        if (heavyWindow or preopenBootstrap) and (not yieldQuote) and shouldAttemptOptionChainZip(
            asof,
            lastChainAsof,
            nowMono,
            zipReadyMono,
            chainFailUntil,
            chainComplete=chainComplete,
            lastZipMono=lastZipMono,
            now=now,
        ):
            refreshed = _tryRefreshOptionChain(logger, xtdata, codes)
            lastZipMono = nowMono
            if refreshed is not None:
                codes = refreshed
                sampleCodes = tuple(codes[:3])
                chainZipFailN = 0
                _setOptionChainCodes(codes)
                if chainComplete:
                    lastChainAsof = asof
                else:
                    grew = optionChainZipGrew(len(codes), seedChainN)
                    if grew:
                        sameSizeHits = 0
                    else:
                        sameSizeHits += 1
                    state = decideOptionChainZipState(grew, sameSizeHits, now)
                    if state == "complete":
                        chainComplete = True
                        lastChainAsof = asof
                        logger.info(
                            "OPT_CHAIN settled n=%d seed=%d grew=%s sameHits=%d",
                            len(codes),
                            seedChainN,
                            grew,
                            sameSizeHits,
                        )
                    else:
                        logger.info(
                            "OPT_CHAIN pending n=%d seed=%d sameHits=%d retry_s=%.0f",
                            len(codes),
                            seedChainN,
                            sameSizeHits,
                            OPT_CHAIN_INCOMPLETE_RETRY_SEC,
                        )
            else:
                chainFailUntil = nowMono + OPT_CHAIN_FAIL_BACKOFF_SEC
                chainZipFailN += 1
                logger.warning(
                    "OPT_CHAIN zip fail n=%d keep retry backoff_s=%.0f",
                    chainZipFailN,
                    OPT_CHAIN_FAIL_BACKOFF_SEC,
                )
        if quotePollOnlyAsof is not None and asof != quotePollOnlyAsof:
            quotePollOnly.clear()
            healFailKey = ()
            healFailN = 0
        quotePollOnlyAsof = asof
        subWanted = optionQuoteSubscribeCodes(
            codes, _copyOptionMeta(), quotePollOnly,
        )
        canTrySub = (not _szoSubUnavailable) or (nowMono >= _szoSubRetryMono)
        syncResult = None
        ownBefore = len(_szoSubOwned)
        if (heavyWindow or preopenBootstrap) and (not yieldQuote) and canTrySub and shouldSyncOptionQuoteSub(
            asof,
            _szoSubConfirmAsof,
            subWanted,
            _szoSubOwned,
            OPT_QUOTE_SUB_MAX_OWNED,
            lastSyncMono=_szoSubLastSyncMono,
            nowMono=nowMono,
            healSec=OPT_QUOTE_SUB_HEAL_SEC if inAuction else 0.0,
            optionCallbackEmpty=bool(
                optPushFallback
                or optPushForceConfirmMono
                or quoteCacheEmptyHits > 0
                or quoteCacheRecheckPending
            ),
        ):
            healUncached = shouldHealUncachedQuoteSub(
                len(_szoSubOwned),
                len(subWanted),
                lastSubCachedN,
                inAuction,
            )
            if healUncached:
                logger.warning(
                    "OPT_SUB heal_uncached owned=%d wanted=%d cached=%d",
                    len(_szoSubOwned),
                    len(subWanted),
                    lastSubCachedN,
                )
            cachedBefore = lastSubCachedN
            syncResult = _syncOptionQuoteSubscriptions(
                logger,
                xtdata,
                subWanted,
                staleSec=120.0 if inAuction else 0.0,
                healUncached=healUncached,
            )
            if syncResult is not None:
                try:
                    lastSubCachedN = int(syncResult.get("cached") or lastSubCachedN)
                except (TypeError, ValueError):
                    pass
                if healUncached:
                    healFailKey, healFailN, giveUp = noteHealUncachedOutcome(
                        cachedBefore,
                        lastSubCachedN,
                        syncResult.get("dropped"),
                        syncResult.get("added"),
                        healFailKey,
                        healFailN,
                    )
                    if giveUp:
                        quotePollOnly.update(giveUp)
                        logger.warning(
                            "OPT_SUB heal_give_up n=%d cached=%d poll_only=%s",
                            len(giveUp),
                            lastSubCachedN,
                            ",".join(giveUp),
                        )
            if syncResult is not None and optionQuoteSubIsHealthy(
                subWanted, _szoSubOwned, OPT_QUOTE_SUB_MAX_OWNED
            ):
                _szoSubConfirmAsof = asof
        addedN = optionSyncAddedN(syncResult)
        deferHeavy = optionHeavyRpcDeferred(addedN, len(_szoSubOwned) > ownBefore)
        # 深市不再走 subscribe_quote（实测 0 回调），1m 下载是主路径，
        # 不能再让订阅状态挡住 1d/闸门。
        szoHealthy = True
        dailyN = 0
        dlN = 0
        # metaMap / TDX / OPT_QTY 已在本轮 yield 前门闩处处理；此处刷新元数据供后续切片
        metaMap = _copyOptionMeta()
        listedToday = optionListedTodayCodes(asof)
        with _cacheLock:
            rotate = int(optionStats["rounds"])
        if heavyWindow:
            # 直播窗：缓存 + 最多 1 片 SHO 轮询（主要是 510050）；禁止整链 1d。
            with _cacheLock:
                etfTickSnap = {c: etfTicks.get(c) for c in OPT_UNDERLYING_QMT}
            hasLiveEtf = etfHasLiveSessionSpot(etfTickSnap, asof)
            if (
                shouldRefreshEtfDailyBars(True, hasLiveEtf)
                and shouldRefreshDailyBars(
                    nowMono, _lastEtfDailyMono, OPT_DAILY_IDLE_SEC, True
                )
            ):
                logger.info("ETF_DAILY fallback no_whole_quote_spot")
                _refreshEtfDailyBars(logger, xtdata)
                _lastEtfDailyMono = nowMono
            dlN = 0
            ok, fail, maxMs = 0, 0, 0.0
            cached = {}
            if codes:
                nowCache = time.monotonic()
                cacheRetrySec = 0.5 if quoteCacheRecheckPending else OPT_QUOTE_CACHE_RETRY_SEC
                canPollCache = hadQuoteCacheHits or (
                    nowCache - lastCachePollMono
                ) >= cacheRetrySec or lastCachePollMono <= 0
                if canPollCache:
                    if OPT_QUOTE_CACHE_GUARD_ENABLE:
                        cached, cacheOutcome = _pollOptionQuoteCache(
                            logger, xtdata, returnOutcome=True,
                        )
                    else:
                        cached = _pollOptionQuoteCache(logger, xtdata)
                        cacheOutcome = "ok" if cached else "authoritative_empty"
                    lastCachePollMono = nowCache
                    cachedN = len(cached) if isinstance(cached, dict) else 0
                    cacheAuthoritative = cacheOutcome in (
                        "ok", "authoritative_empty",
                    )
                    sinceQtySec = (
                        None
                        if lastOptionQtyEndMono <= 0
                        else nowCache - lastOptionQtyEndMono
                    )
                    countCacheEmpty = (
                        shouldCountOptionQuoteCacheEmpty(
                            cacheOutcome,
                            inAuction,
                            sinceQtySec=sinceQtySec,
                        )
                        if OPT_QUOTE_CACHE_GUARD_ENABLE
                        else bool(inAuction and not cached)
                    )
                    # 推送退化统计（2026-09-11）：**必须独立于 cached 是否为空**。
                    # 旧实现只在 `else`（cached 为空）里评估重订，导致
                    # 「有数据但极旧」的退化场景（hit 由 ~140 掉到 16~48）永不触发。
                    if inAuction and cachedN > 0 and lastSubCachedN >= 100:
                        if (cachedN / float(lastSubCachedN)) < OPT_SUB_DEGRADE_RATIO:
                            subDegradeStreak += 1
                        else:
                            subDegradeStreak = 0
                    else:
                        subDegradeStreak = 0
                    if cached:
                        quoteCacheRecheckPending = False
                        if optPushFallback or optPushForceConfirmMono > 0:
                            if optPushRecoverySinceMono <= 0:
                                optPushRecoverySinceMono = nowCache
                                logger.info(
                                    "OPT_SUB dead_push_fallback recover_candidate fresh=%d confirm_s=%.1f",
                                    cachedN, OPT_QUOTE_SUB_RECOVER_CONFIRM_SEC,
                                )
                            if shouldExitOptionPushFallback(
                                cachedN,
                                sinceRecoverySec=nowCache - optPushRecoverySinceMono,
                            ):
                                heldSec = (
                                    nowCache - optPushFallbackSinceMono
                                    if optPushFallbackSinceMono > 0
                                    else nowCache - optPushForceConfirmMono
                                )
                                logger.warning(
                                    "OPT_SUB dead_push_fallback recover owned=%d wanted=%d fresh=%d held_s=%.1f",
                                    len(_szoSubOwned), len(subWanted), cachedN, heldSec,
                                )
                                optPushForceConfirmMono = 0.0
                                optPushFallback = False
                                optPushFallbackSinceMono = 0.0
                                optPushRecoverySinceMono = 0.0
                        else:
                            optPushRecoverySinceMono = 0.0
                        if not optPushFallback and optPushForceConfirmMono <= 0:
                            optPushRecoverySinceMono = 0.0
                        quoteCacheEmptyHits = 0
                        hadQuoteCacheHits = True
                        lastSubCachedN = max(lastSubCachedN, cachedN)
                    elif not inAuction:
                        optPushRecoverySinceMono = 0.0
                        quoteCacheEmptyHits = 0
                        quoteCacheRecheckPending = False
                    elif countCacheEmpty:
                        optPushRecoverySinceMono = 0.0
                        quoteCacheEmptyHits += 1
                        quoteCacheRecheckPending = False
                    else:
                        optPushRecoverySinceMono = 0.0
                        quoteCacheEmptyHits = 0
                        quoteCacheRecheckPending = shouldDeferOptionQuoteCacheRecheck(
                            cacheOutcome,
                            inAuction,
                            sinceQtySec=sinceQtySec,
                        )
                        if inAuction and not cached:
                            logger.info(
                                "OPT_QUOTE_CACHE defer outcome=%s since_qty_s=%s",
                                cacheOutcome,
                                "NA" if sinceQtySec is None else f"{sinceQtySec:.2f}",
                            )
                    cacheHealthDegraded = bool(
                        quoteCacheEmptyHits >= OPT_QUOTE_CACHE_EMPTY_RESYNC
                        or subDegradeStreak >= OPT_SUB_DEGRADE_STREAK
                    )
                    sinceForce = (
                        None
                        if lastForceMono <= 0
                        else (nowCache - lastForceMono)
                    )
                    if cacheAuthoritative and cacheHealthDegraded and shouldEnterOptionPushFallback(
                        len(_szoSubOwned),
                        len(subWanted),
                        cachedN,
                        inAuction,
                        bool(optPushForceConfirmMono),
                        sinceForceSec=(
                            None
                            if optPushForceConfirmMono <= 0
                            else nowCache - optPushForceConfirmMono
                        ),
                    ):
                        optPushFallback = True
                        optPushFallbackSinceMono = nowCache
                        optPushForceConfirmMono = 0.0
                        optPushRecoverySinceMono = 0.0
                        logger.warning(
                            "OPT_SUB dead_push_fallback enter owned=%d wanted=%d fresh=%d "
                            "confirm_s=%.1f; suppress force churn, keep bounded tick/TDX fallback",
                            len(_szoSubOwned), len(subWanted), cachedN,
                            OPT_QUOTE_SUB_FORCE_CONFIRM_SEC,
                        )
                    if (not optPushFallback) and optPushForceConfirmMono <= 0 and shouldForceQuoteSubResync(
                        cachedN,
                        quoteCacheEmptyHits,
                        OPT_QUOTE_CACHE_EMPTY_RESYNC,
                        isSubFull=optionQuoteSubIsHealthy(
                            subWanted, _szoSubOwned, OPT_QUOTE_SUB_MAX_OWNED
                        ),
                        inAuction=inAuction,
                        sinceForceSec=sinceForce,
                        forceCooldownSec=OPT_QUOTE_SUB_FORCE_COOLDOWN_SEC,
                        cachedN=lastSubCachedN,
                        degradedStreak=subDegradeStreak,
                        cacheAuthoritative=cacheAuthoritative,
                        healthDegraded=cacheHealthDegraded,
                    ):
                        logger.warning(
                            "OPT_SUB force resync cache_empty=%d degrade_streak=%d "
                            "hit=%d/%d",
                            quoteCacheEmptyHits,
                            subDegradeStreak,
                            cachedN,
                            lastSubCachedN,
                        )
                        quoteCacheEmptyHits = 0
                        subDegradeStreak = 0
                        lastForceMono = nowCache
                        if canTrySub:
                            syncResult = _syncOptionQuoteSubscriptions(
                                logger,
                                xtdata,
                                subWanted,
                                force=True,
                                staleSec=120.0,
                            )
                            addedN = optionSyncAddedN(syncResult)
                            deferHeavy = False
                            if syncResult is not None:
                                optPushForceConfirmMono = nowCache
                    # syncResult.cached 是桥接缓存成员数，可能全是旧事件；健康判定
                    # 只能使用本轮 get_single_quote_ticks 已验证的新鲜缓存数。
                    if cacheAuthoritative and cacheHealthDegraded:
                        _optHealthDeadPushMono = maybeLogOptHealthDeadPush(
                            logger,
                            len(_szoSubOwned),
                            len(subWanted),
                            cachedN,
                            inAuction,
                            _optHealthDeadPushMono,
                            nowCache,
                        )
                if deferHeavy and not isWholeQuoteHealthyForOptDl(uniqueN, silenceSec):
                    logger.info(
                        "OPT_ROUND defer added=%d skip tick/1m cache=%d",
                        addedN,
                        len(cached),
                    )
                else:
                    # 先读取 OI 压力快照，再决定是否启动 510050 的非关键
                    # live tick 灌库；159915 分钟 OI 仍保留，避免让行门反过来
                    # 饿死主要的 SZO OI 候选路径。
                    gateMono = time.monotonic()
                    oiGate = optionChunkOiGateDecision(
                        optionQtyPressureSnapshot(),
                        nowMono=gateMono,
                        lastActionMono=_optChunkOiGateLastActionMono,
                        inWindow=heavyWindow,
                    )
                    if oiGate["defer"] or oiGate["probe"]:
                        _optChunkOiGateLastActionMono = oiGate["nextActionMono"]
                    else:
                        _optChunkOiGateLastActionMono = 0.0
                    if oiGate["defer"] and OPT_CHUNK_OI_GATE_LOG_SEC > 0 and (
                        gateMono - _optChunkOiGateLastLogMono
                    ) >= OPT_CHUNK_OI_GATE_LOG_SEC:
                        logger.info(
                            "OPT_CHUNK_OI_GATE defer reason=%s demand=%d "
                            "wait_p95=%.1fs p0_oi_max=%.1fs gap=%.1fs "
                            "snapshot_age=%.1fs probe_in=%.1fs",
                            oiGate["reason"],
                            oiGate["demandN"],
                            oiGate["waitP95Sec"],
                            oiGate["p0OiMaxSec"],
                            oiGate["gapSec"],
                            oiGate["snapshotAgeSec"]
                            if oiGate["snapshotAgeSec"] is not None
                            else -1.0,
                            max(
                                0.0,
                                float(OPT_CHUNK_OI_GATE_PROBE_INTERVAL_SEC)
                                - max(
                                    0.0,
                                    gateMono
                                    - float(
                                        oiGate["nextActionMono"]
                                        or gateMono
                                    ),
                                ),
                            ),
                        )
                        _optChunkOiGateLastLogMono = gateMono
                    elif oiGate["probe"] and OPT_CHUNK_OI_GATE_LOG_SEC > 0 and (
                        gateMono - _optChunkOiGateLastLogMono
                    ) >= OPT_CHUNK_OI_GATE_LOG_SEC:
                        logger.info(
                            "OPT_CHUNK_OI_GATE probe reason=%s demand=%d "
                            "wait_p95=%.1fs p0_oi_max=%.1fs gap=%.1fs",
                            oiGate["reason"],
                            oiGate["demandN"],
                            oiGate["waitP95Sec"],
                            oiGate["p0OiMaxSec"],
                            oiGate["gapSec"],
                        )
                        _optChunkOiGateLastLogMono = gateMono
                    if oiGate["defer"] or oiGate["probe"]:
                        # 510050 已由 TDX 主供，压力/探测窗不再额外发 live
                        # tick 下载；否则下载后的回读仍会重新占用同一 RPC。
                        dlN = 0
                    else:
                        dlN = _maybeDownloadLiveStaleOptionTicks(
                            logger, xtdata, codes, asof, uniqueN, silenceSec,
                        )
                    dlN += _maybeDownloadSzoLiveTicksChunk(
                        logger, xtdata, codes, asof, uniqueN, silenceSec,
                    )
                    pollSrc = interleaveOptionCodesByUnderlying(
                        optionShoCodes(codes),
                        metaMap,
                    )
                    pollParams = optPollChunkParams()
                    if oiGate["probe"]:
                        # 压力持续时只放行压力档的一片，不能借探测名义恢复整轮。
                        pollParams = optPollChunkParams(underPressure=True)
                    pollCodes = optionShoPollBudget(
                        optionShoLivePollCodes(
                            pollSrc,
                            _copyOptionTicks(),
                            cached,
                            asof,
                            OPT_QUOTE_CACHE_MAX_AGE_SEC,
                        ),
                        pollParams["chunkSize"],
                        pollParams["maxChunks"],
                        rotate,
                    )
                    pollCodes = optionShoPollSkipTdxCovered(pollCodes, metaMap)
                    if pollCodes:
                        if oiGate["defer"]:
                            ok, fail, maxMs = 0, 0, 0.0
                        else:
                            ok, fail, maxMs = _pollOptionRound(
                                logger, xtdata, pollCodes, sampleCodes, metaMap, rotate,
                                maxChunks=pollParams["maxChunks"],
                                chunkSize=pollParams["chunkSize"],
                                yieldSec=pollParams["yieldSec"],
                                timeoutSec=pollParams["timeoutSec"],
                            )
                        if fail:
                            noteOptChunkRound(fail)
                            if isOptChunkUnderPressure():
                                logger.info(
                                    "OPT_CHUNK pressure on fail=%d size=%d maxChunks=%d",
                                    fail,
                                    pollParams["chunkSize"],
                                    pollParams["maxChunks"],
                                )
                    missSzo = optionSzoMinuteNeedCodes(
                        codes,
                        _copyOptionTicks(),
                        asof,
                        now=now,
                        staleSec=OPT_SZO_MIN_STALE_SEC,
                        metaMap=metaMap,
                        skipUntil=szoMinFailUntil,
                        nowMono=nowMono,
                        spotByUndl=etfSpotByUndlSix(),
                    )
                    if missSzo:
                        _applySzoMinuteQuotes(
                            logger, xtdata, missSzo, asof,
                            szoLiveTickDownloadStartYmd(asof),
                            rpcFallback=False,
                        )
                        szoMinFailUntil = optionSzoMinuteMarkFailed(
                            missSzo,
                            _copyOptionTicks(),
                            asof,
                            nowMono,
                            OPT_SZO_MIN_FAIL_BACKOFF_SEC,
                            szoMinFailUntil,
                        )
                    if shouldFillOptionMetaInPoll(
                        False,
                        bool(oiGate["defer"] or oiGate["probe"]),
                        deferHeavy,
                        isWholeQuoteHealthyForOptDl(uniqueN, silenceSec),
                    ):
                        # 直播窗窄口补 optMeta，供 T 型补行
                        _fillOptionMetaGaps(
                            logger, xtdata, optionMetaFillBatchSize(now),
                        )
        else:
            inCatchup = isOptionCatchupWindow(now)
            # 盘后不得再发 QMT 补齐 RPC。最后市场事件已经冻结，继续 get_market_data_ex
            # 只会让主桥显示 BUSY，并无助于页面新鲜度；盘前仍允许做必要启动基线。
            skipHeavy = (not preopenBootstrap) or shouldSkipHeavyUntilQuoteSubFull(
                subWanted, _szoSubOwned, OPT_QUOTE_SUB_MAX_OWNED
            )
            if preopenBootstrap and skipHeavy:
                logger.info(
                    "OPT_DL skip sub_not_full owned=%d wanted=%d",
                    len(_szoSubOwned),
                    len(subWanted),
                )
            if preopenBootstrap and (not skipHeavy) and shouldRefreshDailyBars(
                nowMono, _lastEtfDailyMono, OPT_DAILY_LIVE_SEC, False
            ):
                _refreshEtfDailyBars(logger, xtdata)
                _lastEtfDailyMono = nowMono
            if preopenBootstrap and (not skipHeavy) and shouldRefreshDailyBars(
                nowMono, _lastOptDailyMono, OPT_DAILY_LIVE_SEC, False
            ):
                dailyN = _refreshOptionDailyWithGates(
                    logger, xtdata, codes, asof, szoHealthy, _szoSubUnavailable,
                    owned=_szoSubOwned, listedToday=listedToday, rotate=rotate,
                )
                _lastOptDailyMono = nowMono
            ok, fail, maxMs = 0, 0, 0.0
            dlN = 0
            cached = {}
            if inCatchup and codes and not yieldQuote and not skipHeavy:
                cached = _pollOptionQuoteCache(
                    logger, xtdata, maxAgeSec=OPT_QUOTE_CACHE_CATCHUP_AGE_SEC,
                )
                pollSrc = interleaveOptionCodesByUnderlying(
                    optionLivePollCodes(codes), metaMap,
                )
                catchParams = optPollChunkParams()
                # 后盘回捞仍允许多片，但压力期收紧片宽与超时
                catchMax = min(
                    int(OPT_CATCHUP_CHUNKS),
                    max(1, int(catchParams["maxChunks"]) * 4),
                )
                if not isOptChunkUnderPressure():
                    catchMax = OPT_CATCHUP_CHUNKS
                pollCodes = optionShoPollBudget(
                    optionCacheMissCodes(
                        pollSrc, cached, OPT_QUOTE_CACHE_CATCHUP_AGE_SEC
                    ),
                    catchParams["chunkSize"],
                    catchMax,
                    rotate,
                )
                pollCodes = optionShoPollSkipTdxCovered(pollCodes, metaMap)
                if pollCodes:
                    ok, fail, maxMs = _pollOptionRound(
                        logger, xtdata, pollCodes, sampleCodes, metaMap, rotate,
                        maxChunks=catchMax,
                        chunkSize=catchParams["chunkSize"],
                        yieldSec=catchParams["yieldSec"],
                        timeoutSec=catchParams["timeoutSec"],
                    )
                    if fail:
                        noteOptChunkRound(fail)
                missSzo = optionSzoMinuteNeedCodes(
                    codes,
                    _copyOptionTicks(),
                    asof,
                    now=now,
                    staleSec=None,
                    metaMap=metaMap,
                    skipUntil=szoMinFailUntil,
                    nowMono=nowMono,
                    spotByUndl=etfSpotByUndlSix(),
                )
                if missSzo:
                    triedSzo = optionShoPollBudget(
                        missSzo, OPT_SZO_MIN_DL_CHUNK, 1, rotate
                    )
                    _maybeDownloadSzoLiveTicks(logger, xtdata, triedSzo, asof)
                    szoMinFailUntil = optionSzoMinuteMarkFailed(
                        triedSzo,
                        _copyOptionTicks(),
                        asof,
                        nowMono,
                        OPT_SZO_MIN_FAIL_BACKOFF_SEC,
                        szoMinFailUntil,
                    )
            if preopenBootstrap and (not skipHeavy) and shouldDownloadOptionTicks(now):
                dlN = _maybeDownloadStaleOptionTicks(logger, xtdata, codes, asof)
            if preopenBootstrap and shouldFillOptionMetaInPoll(yieldQuote, skipHeavy):
                _fillOptionMetaGaps(
                    logger, xtdata, optionMetaFillBatchSize(now),
                )
        elapsed = time.perf_counter() - t0
        stillNeed = []
        if (not heavyWindow) and shouldDownloadOptionTicks(now):
            stillNeed = optionLiveTickGapCodes(
                [
                    c
                    for c in optionCodesNeedingTickDownload(
                        codes, _copyOptionTicks(), asof, _copyOptionDaily()
                    )
                    if c not in _tickDlTried(asof)
                ],
                optionListedTodayCodes(asof),
            )
        with _cacheLock:
            optionStats["rounds"] += 1
            optionStats["lastRoundSec"] = elapsed
            optionStats["lastChunkMs"] = maxMs
            optionStats["failChunks"] += fail
            rounds = optionStats["rounds"]
        if fail:
            emitCooldownLog(
                logger, "OPT_FAIL", logging.WARNING,
                "OPT_ROUND rounds=%d ok=%d fail_chunks=%d daily=%d dl=%d "
                "elapsed_s=%.1f max_chunk_ms=%.0f remain_dl=%d",
                rounds,
                ok,
                fail,
                dailyN,
                dlN,
                elapsed,
                maxMs,
                len(stillNeed),
            )
        elif not heavyWindow:
            logger.info(
                "OPT_%s ticks=%d daily=%d dl=%d cache=%d remain_dl=%d",
                "CATCHUP" if isOptionCatchupWindow(now) else "IDLE",
                ok,
                dailyN,
                dlN,
                len(cached) if cached else 0,
                len(stillNeed),
            )
        waitSec = optionLivePollWaitSec(
            heavyWindow,
            time.monotonic(),
            _lastOptDailyMono,
            downloadTicks=(not heavyWindow) and shouldDownloadOptionTicks(now),
            stillNeedN=len(stillNeed),
            dlN=dlN,
            catchup=(not heavyWindow) and isOptionCatchupWindow(now),
            failChunks=fail,
        )
        _sleepInterruptible(waitSec)


def _ensureXtquantSrc() -> None:
    src = XTQUANT_SRC
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)


def _setupLogging(logger: logging.Logger) -> None:
    logger.setLevel(logging.INFO)
    logFormat = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormat)
    logger.addHandler(consoleHandler)
    logPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "大QMT实时行情.log")
    fileHandler = WindowsSafeTimedRotatingFileHandler(
        logPath, when="midnight", interval=1, backupCount=3, encoding="utf-8"
    )
    fileHandler.setFormatter(logFormat)
    fileHandler.suffix = "%Y%m%d"
    logger.addHandler(fileHandler)


def _onQuote(data) -> None:
    payload = data or {}
    n = len(payload)
    if not n:
        return
    nowMono = time.monotonic()
    with _cacheLock:
        prev = stats["lastPushMono"]
        if prev:
            gapMs = (nowMono - prev) * 1000.0
            if gapMs > stats["maxGapMs"]:
                stats["maxGapMs"] = gapMs
        stats["lastPushMono"] = nowMono
        stats["lastPushWall"] = datetime.now()
        stats["batches"] += 1
        stats["tickUpdates"] += n
        stats["uniqueCodes"].update(payload.keys())
        for code in ETF_TICK_CODE_SET:
            tick = payload.get(code)
            if tick is not None:
                etfTicks[code] = tick
        for code in SAMPLE_CODES:
            tick = payload.get(code)
            if tick is None:
                continue
            stats["sampleTicks"] += 1
            if tickHasFiveBand(tick):
                stats["sampleFiveBand"] += 1


def _rpcPing(xtTrader, timeoutSec: float = PING_WATCHDOG_TIMEOUT_SEC):
    """返回 (ping_ms, payload)。payload 缺字段时仍是 dict。"""
    with _rpcLock:
        t0 = time.perf_counter()
        data = xtTrader.client.call("ping", {}, timeout_seconds=timeoutSec)
        ms = round((time.perf_counter() - t0) * 1000.0, 1)
        payload = data if isinstance(data, dict) else {}
        return ms, payload


def _rpcPingMs(xtTrader, timeoutSec: float = PING_WATCHDOG_TIMEOUT_SEC) -> float:
    ms, _ = _rpcPing(xtTrader, timeoutSec=timeoutSec)
    return ms


def tryRpcPingMs(
    xtTrader,
    timeoutSec: float = PING_WATCHDOG_TIMEOUT_SEC,
    lockWaitSec: float = RPC_LOCK_PING_WAIT_SEC,
):
    """锁被期权 RPC 占用时立刻放弃，避免心跳/假活再堵 12s。返回 (ms, err)。"""
    got = _rpcLock.acquire(timeout=max(0.0, float(lockWaitSec)))
    if not got:
        return None, "busy"
    try:
        t0 = time.perf_counter()
        xtTrader.client.call("ping", {}, timeout_seconds=timeoutSec)
        return round((time.perf_counter() - t0) * 1000.0, 1), None
    except Exception as exc:
        return None, str(exc)
    finally:
        _rpcLock.release()


def isTcpPortOpen(host: str, port: int, timeoutSec: float = 0.4) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(max(0.05, float(timeoutSec)))
    try:
        return sock.connect_ex((str(host), int(port))) == 0
    except OSError:
        return False
    finally:
        sock.close()


def waitUntilRpcPing(logger, xtTrader) -> bool:
    """桥接未听或 ROUTER 刚被抢锁时等到 ping 通。Ctrl+C 可退。"""
    lastLog = -RPC_WAIT_LOG_SEC
    t0 = time.monotonic()
    host = QUOTE_RPC_HOST
    port = QUOTE_RPC_PORT
    logger.info(
        "等待桥接 RPC %s:%s ping超时=%.1fs",
        host, port, RPC_PROBE_TIMEOUT_SEC,
    )
    while running:
        portOpen = isTcpPortOpen(host, port)
        if portOpen:
            try:
                pingMs, payload = _rpcPing(xtTrader, timeoutSec=RPC_PROBE_TIMEOUT_SEC)
                revisionOk = applyBridgePingPayload(payload)
                logger.info(
                    "RPC 就绪 ping_ms=%.1f waited_s=%.1f %s:%s "
                    "revision=%s owned=%s cached=%s",
                    pingMs,
                    time.monotonic() - t0,
                    host,
                    port,
                    payload.get("rpc_revision") or "-",
                    payload.get("owned"),
                    payload.get("cached"),
                )
                if not revisionOk:
                    logger.error(
                        "RPC revision mismatch got=%s want=%s "
                        "期权 subscribe_quote 将拒绝",
                        _bridgeRevision or "-",
                        EXPECTED_RPC_REVISION,
                    )
                return True
            except Exception as exc:
                msg = "ping 失败: %s" % exc
        else:
            msg = "桥接端口未听 %s:%s" % (host, port)
        now = time.monotonic()
        if now - lastLog >= RPC_WAIT_LOG_SEC:
            logger.warning("等待桥接 RPC：%s", msg)
            lastLog = now
        _sleepInterruptible(RPC_WAIT_SLEEP_SEC)
    return False


def subscribeWholeQuoteUntilOk(logger, xtdata, codes, force=True):
    """订全推直到 RPC 接受；回调是否真实到达由 watchdog 独立确认。

    RPC 很快返回只可作为诊断信号，不能在启动期直接退订：QMT bridge 可以异步
    接受订阅，随后才投递 callback。若实际未产生回调，watchdog 会按静默门限重订。
    """
    lastLog = -RPC_WAIT_LOG_SEC
    t0 = time.monotonic()
    while running:
        try:
            tSub = time.perf_counter()
            subId = xtdata.subscribe_whole_quote(
                codes, callback=_onQuote, prime=False, force=force,
            )
            elapsed = time.perf_counter() - tSub
            with _cacheLock:
                stats["subscribedMono"] = time.monotonic()
            logger.info(
                "已订阅 sub_id=%s prime=False force=%s elapsed_s=%.2f waited_s=%.1f",
                subId, force, elapsed, time.monotonic() - t0,
            )
            if subscribeLooksNoop(elapsed):
                logger.info(
                    "SUBSCRIBE_FAST_ACK elapsed_s=%.3f keep sub_id=%s，等待 watchdog 回调确认",
                    elapsed,
                    subId,
                )
            return subId
        except Exception as exc:
            now = time.monotonic()
            if now - lastLog >= RPC_WAIT_LOG_SEC:
                logger.warning("全推订阅失败，将重试: %s", exc)
                lastLog = now
            sleepSec = SUBSCRIBE_RETRY_SLEEP_SEC
            if not isTcpPortOpen(QUOTE_RPC_HOST, QUOTE_RPC_PORT):
                sleepSec = RPC_WAIT_SLEEP_SEC
            _sleepInterruptible(sleepSec)
    return None


def _tryForceResub(logger, xtdata, codes, subIdBox, settleSec: float = 0.4) -> bool:
    """期权让路后真退真订，force 拆死句柄。空订视为失败，不进入确认成功。"""
    _quoteHold.set()
    try:
        got = _rpcLock.acquire(timeout=8.0)
        if not got:
            logger.warning("RESUB_FAIL 期权 RPC 未让出锁")
            return False
        try:
            oldId = subIdBox.get("v")
            if oldId is not None:
                try:
                    xtdata.unsubscribe_quote(oldId)
                except Exception as exc:
                    logger.error("RESUB_FAIL 退订异常: %s", exc)
                    return False
                subIdBox["v"] = None
            time.sleep(max(0.4, float(settleSec)))
            t0 = time.perf_counter()
            try:
                newId = xtdata.subscribe_whole_quote(
                    codes, callback=_onQuote, prime=False, force=True,
                )
            except Exception as exc:
                logger.error("RESUB_FAIL 再订异常: %s", exc)
                return False
            elapsed = time.perf_counter() - t0
            subIdBox["v"] = newId
            with _cacheLock:
                stats["subscribedMono"] = time.monotonic()
                stats["uniqueCodes"].clear()
                stats["lastPushMono"] = 0.0
                stats["lastPushWall"] = None
            if subscribeLooksNoop(elapsed):
                emitCooldownLog(
                    logger, "RESUB_NOOP", logging.WARNING,
                    "RESUB_NOOP elapsed_s=%.3f sub_id=%s（桥接未 force 或 combo 未拆）",
                    elapsed,
                    newId,
                )
                return False
            logger.info("RESUB_WAIT 已再订 sub_id=%s elapsed_s=%.2f", newId, elapsed)
            return True
        finally:
            _rpcLock.release()
    finally:
        _quoteHold.clear()


def _maybeProbeEtfTicks(
    logger, xtdata, nowWall, nowMono, uniqueN, lastProbeMono, silenceSec: float = 0.0,
):
    """全推无增量时用 5 只 ETF 探针续盘口。返回新的 lastProbeMono。"""
    if not isOptionPollWindow(nowWall):
        return lastProbeMono
    interval = etfProbeIntervalSec(isWatchdogWindow(nowWall))
    if not shouldProbeEtfTicks(
        uniqueN, nowMono, lastProbeMono, interval, silenceSec=silenceSec,
    ):
        return lastProbeMono
    got = _rpcLock.acquire(timeout=0.15)
    if not got:
        return lastProbeMono
    try:
        payload = probeEtfWholeQuoteTicks(xtdata)
    except Exception as exc:
        logger.warning("ETF_PROBE fail %s", exc)
        payload = {}
    finally:
        _rpcLock.release()
    n = applyEtfProbeTicks(payload)
    if n:
        logger.info("ETF_PROBE n=%d unique=%d", n, uniqueN)
    return nowMono


def _runWatchdogLoop(logger, xtTrader, xtdata, codes, subIdBox) -> None:
    consecutiveFail = 0
    lastHeartbeat = time.monotonic()
    lastGiveUpLog = 0.0
    giveUpSince = 0.0
    confirmSince = 0.0
    lastEtfProbeMono = 0.0
    lastInstanceCheck = 0.0
    lastPeerLog = 0.0
    busResubN = 0
    lockPath = defaultInstanceLockPath()
    while running:
        _sleepInterruptible(WATCHDOG_SLEEP_SEC)
        if not running:
            break
        nowMono = time.monotonic()
        nowWall = datetime.now()
        if nowMono - lastHeartbeat >= HEARTBEAT_SEC:
            pingTxt = "-"
            pingMs, pingErr = tryRpcPingMs(xtTrader, lockWaitSec=0.05)
            if pingMs is not None:
                pingTxt = "%.1f" % pingMs
            elif pingErr == "busy":
                pingTxt = "BUSY"
            else:
                pingTxt = "FAIL:%s" % pingErr
            logger.info("%s ping_ms=%s", _heartbeatLine(), pingTxt)
            lastHeartbeat = nowMono
        if nowMono - lastInstanceCheck >= INSTANCE_RECHECK_SEC:
            killed = enforceSoleQuoteInstance(logger, lockPath)
            peers = scanQuoteLivePids()
            if peers and (nowMono - lastPeerLog) >= INSTANCE_RECHECK_SEC:
                logger.error(
                    "QUOTE_PEER_CONFLICT quote-live pid=%s 会抢 7214 combo",
                    peers,
                )
                lastPeerLog = nowMono
            if killed:
                logger.warning("运行中清掉漏网本脚本 pid=%s", killed)
            lastInstanceCheck = nowMono
        with _cacheLock:
            lastPush = stats["lastPushMono"]
            subscribedMono = float(stats.get("subscribedMono") or 0.0)
            uniqueN = len(stats["uniqueCodes"])
        silenceSec = effectiveSilenceSec(
            nowWall, nowMono, lastPush, subscribedMono,
        )
        if (
            confirmSince <= 0
            and isWatchdogWindow(nowWall)
            and silenceSec >= SILENCE_SECONDS
            and uniqueN
        ):
            with _cacheLock:
                stats["uniqueCodes"].clear()
                uniqueN = 0
            logger.info("STALE_UNIQUE 静默 %.1fs，清空 unique 以免假活", silenceSec)
        lastEtfProbeMono = _maybeProbeEtfTicks(
            logger, xtdata, nowWall, nowMono, uniqueN, lastEtfProbeMono,
            silenceSec=silenceSec,
        )
        if confirmSince > 0:
            confirmSince, consecutiveFail, giveUpSince = _advanceResubConfirm(
                logger, nowMono, confirmSince, lastPush, silenceSec, subIdBox,
                consecutiveFail, giveUpSince,
            )
            continue
        if silenceSec < SILENCE_SECONDS:
            consecutiveFail = 0
            giveUpSince = 0.0
            markOptRpcBusy(False)
        action = decideWatchdogAction(
            isWatchdogWindow(nowWall),
            silenceSec,
            consecutiveFail,
        )
        if action == "idle":
            continue
        if action == "give_up":
            consecutiveFail, giveUpSince, lastGiveUpLog = _advanceGiveUp(
                logger, nowMono, consecutiveFail, giveUpSince, lastGiveUpLog, silenceSec,
            )
            continue
        # 快捷路径：unique 长期只有个位数 ⇒ combo 根本没订上，与 ping 快慢无关。
        # 必须抢在 ping 之前（ping 会因 _rpcLock 被期权 RPC 占用而返回 busy，
        # 2026-09-11 现场正因如此一直卡在 PING_BUSY，永不重订）。
        if quoteUniqueCriticallyLow(uniqueN):
            logger.warning(
                "假活 unique 极低强制重订 unique=%d silence_s=%.1f（跳过 ping）",
                uniqueN, silenceSec,
            )
            settle = RESUB_NOOP_SETTLE_SEC if consecutiveFail >= 2 else 0.4
            if _tryForceResub(logger, xtdata, codes, subIdBox, settleSec=settle):
                confirmSince = time.monotonic()
            else:
                consecutiveFail += 1
                with _cacheLock:
                    stats["resubFail"] += 1
            continue
        pingMs, pingErr = tryRpcPingMs(xtTrader)
        if pingMs is None:
            if pingErr == "busy":
                busResubN += 1
                markOptRpcBusy(True)
                silenceBusy = effectiveSilenceSec(
                    datetime.now(),
                    time.monotonic(),
                    stats["lastPushMono"],
                    float(stats.get("subscribedMono") or 0.0),
                )
                if shouldResubOnBusySilence(busResubN, silenceBusy):
                    logger.warning(
                        "假活 ping 持续 busy 强制重订 busy_n=%d silence_s=%.1f",
                        busResubN, silenceBusy,
                    )
                    settle = RESUB_NOOP_SETTLE_SEC if consecutiveFail >= 2 else 0.4
                    if _tryForceResub(logger, xtdata, codes, subIdBox, settleSec=settle):
                        confirmSince = time.monotonic()
                    else:
                        consecutiveFail += 1
                        with _cacheLock:
                            stats["resubFail"] += 1
                    busResubN = 0
                else:
                    logger.info(
                        "PING_BUSY 期权让路 silence_s=%.1f busy_n=%d",
                        silenceBusy, busResubN,
                    )
            else:
                busResubN = 0
                logger.warning("PING_FAIL %s silence_s=%.1f", pingErr, silenceSec)
            continue
        busResubN = 0
        markOptRpcBusy(False)
        with _cacheLock:
            lastPush = stats["lastPushMono"]
            subscribedMono = float(stats.get("subscribedMono") or 0.0)
        silenceSec = effectiveSilenceSec(
            datetime.now(), time.monotonic(), lastPush, subscribedMono,
        )
        if not shouldTreatAsFakeAlive(pingMs, silenceSec, uniqueN=uniqueN):
            if (
                pingMs > PING_FAKE_ALIVE_MAX_MS
                and silenceSec >= SILENCE_SECONDS
                and uniqueN >= QUOTE_CRITICAL_UNIQUE_N
            ):
                markOptRpcBusy(True)
                logger.info(
                    "假活先让路 ping_ms=%.1f silence_s=%.1f unique=%d",
                    pingMs, silenceSec, uniqueN,
                )
            else:
                logger.info(
                    "假活跳过 ping_ms=%.1f silence_s=%.1f unique=%d（已有增量）",
                    pingMs, silenceSec, uniqueN,
                )
            continue
        if pingMs > PING_FAKE_ALIVE_MAX_MS:
            logger.warning(
                "假活慢 ping 强制重订 ping_ms=%.1f silence_s=%.1f unique=%d"
                "（unique 长期个位数=combo 未订上）",
                pingMs, silenceSec, uniqueN,
            )
        logger.info(
            "假活检测 ping_ms=%.1f silence_s=%.1f unique=%d，尝试真退真订",
            pingMs, silenceSec, uniqueN,
        )
        settle = RESUB_NOOP_SETTLE_SEC if consecutiveFail >= 2 else 0.4
        if _tryForceResub(logger, xtdata, codes, subIdBox, settleSec=settle):
            confirmSince = time.monotonic()
        else:
            consecutiveFail += 1
            with _cacheLock:
                stats["resubFail"] += 1


def _advanceResubConfirm(
    logger, nowMono, confirmSince, lastPush, silenceSec, subIdBox,
    consecutiveFail, giveUpSince,
):
    verdict = decideResubConfirm(nowMono, confirmSince, lastPush)
    if verdict == "ok":
        with _cacheLock:
            stats["resubOk"] += 1
        logger.info("RESUB_OK sub_id=%s", subIdBox.get("v"))
        return 0.0, 0, 0.0
    if verdict == "fail":
        logger.error(
            "RESUB_FAIL 未见增量 silence_s=%.1f confirm_s=%.1f",
            silenceSec,
            nowMono - confirmSince,
        )
        with _cacheLock:
            stats["resubFail"] += 1
        return 0.0, consecutiveFail + 1, giveUpSince
    return confirmSince, consecutiveFail, giveUpSince


def _advanceGiveUp(logger, nowMono, consecutiveFail, giveUpSince, lastGiveUpLog, silenceSec):
    if giveUpSince <= 0:
        giveUpSince = nowMono
    if nowMono - lastGiveUpLog >= GIVE_UP_LOG_SEC:
        logger.error(
            "RESUB_GIVE_UP consecutive_fail=%d silence_s=%.1f，%ds 后自动再试",
            consecutiveFail,
            silenceSec,
            int(GIVE_UP_RETRY_SEC),
        )
        lastGiveUpLog = nowMono
    if decideGiveUpNext(nowMono, giveUpSince) == "retry":
        logger.info("RESUB_RETRY 放弃态冷却结束，恢复真退真订检测")
        return 0, 0.0, 0.0
    return consecutiveFail, giveUpSince, lastGiveUpLog


def _heartbeatLine() -> str:
    with _cacheLock:
        lastWall = stats["lastPushWall"]
        uniqueN = len(stats["uniqueCodes"])
        batches = stats["batches"]
        updates = stats["tickUpdates"]
        maxGap = stats["maxGapMs"]
        resubOk = stats["resubOk"]
        resubFail = stats["resubFail"]
        five = stats["sampleFiveBand"]
        sampleN = stats["sampleTicks"]
        probeN = int(stats.get("etfProbeN") or 0)
        optLast = optionStats["lastWall"]
        optRounds = optionStats["rounds"]
        optOk = optionStats["okTicks"]
        optUnique = len(optionStats["uniqueCodes"])
        optRoundSec = optionStats["lastRoundSec"]
        optChunkMs = optionStats["lastChunkMs"]
        optFail = optionStats["failChunks"]
        optFive = optionStats["sampleFiveBand"]
        optSampleN = optionStats["sampleTicks"]
        lastPushMono = float(stats.get("lastPushMono") or 0.0)
        subscribedMono = float(stats.get("subscribedMono") or 0.0)
    lastTxt = lastWall.strftime("%H:%M:%S") if lastWall else "-"
    fiveTxt = ("%d/%d" % (five, sampleN)) if sampleN else "-"
    optLastTxt = optLast.strftime("%H:%M:%S") if optLast else "-"
    optFiveTxt = ("%d/%d" % (optFive, optSampleN)) if optSampleN else "-"
    silenceSec = effectiveSilenceSec(
        datetime.now(), time.monotonic(), lastPushMono, subscribedMono,
    )
    return (
        "heartbeat batches=%d updates=%d unique=%d silence_s=%.1f max_gap_ms=%.1f "
        "resub_ok=%d resub_fail=%d last_push=%s five_band=%s etf_probe=%d "
        "opt_rounds=%d opt_ok=%d opt_unique=%d opt_round_s=%.1f "
        "opt_chunk_ms=%.0f opt_fail=%d opt_last=%s opt_five=%s"
        % (
            batches, updates, uniqueN, silenceSec, maxGap, resubOk, resubFail,
            lastTxt, fiveTxt, probeN,
            optRounds, optOk, optUnique, optRoundSec, optChunkMs, optFail,
            optLastTxt, optFiveTxt,
        )
    )


def _runShutdownStep(logger: logging.Logger, phase: str, action: Callable[[], object]) -> bool:
    started = time.monotonic()
    logger.info("SHUTDOWN_STEP phase=%s state=begin", phase)
    try:
        action()
    except Exception as exc:
        logger.warning(
            "SHUTDOWN_STEP phase=%s state=failed elapsed_s=%.3f error=%s",
            phase, time.monotonic() - started, exc,
        )
        return False
    else:
        logger.info(
            "SHUTDOWN_STEP phase=%s state=done elapsed_s=%.3f",
            phase, time.monotonic() - started,
        )
        return True


def _joinQuoteWorker(worker: threading.Thread, timeoutSec: float) -> None:
    worker.join(timeout=timeoutSec)
    if worker.is_alive():
        raise TimeoutError("%s 等待 %.1fs 后仍在执行" % (worker.name, timeoutSec))


def _stopQuoteHttp(server: ThreadingHTTPServer) -> None:
    try:
        server.shutdown()
    finally:
        server.server_close()


def main() -> int:
    global running, _stopLogged
    running, _stopLogged = True, False
    logger = logging.getLogger("daqmt_rt_quote")
    _setupLogging(logger)

    def handleStop(signum, _frame):
        global running, _stopLogged
        running = False
        if not _stopLogged:
            logger.info("收到信号 %s，准备退订退出", signum)
            _stopLogged = True

    signal.signal(signal.SIGINT, handleStop)
    signal.signal(signal.SIGTERM, handleStop)

    serviceDone = threading.Event()
    exitCode = [1]

    def runService() -> None:
        try:
            exitCode[0] = _runQuoteService(logger)
        except Exception:
            logger.exception("QUOTE_SERVICE failed")
        finally:
            serviceDone.set()

    # 信号只在主线程处理。服务线程沿用原有串行生命周期，主线程始终可响应
    # 停止；QMT 请求/锁卡住时，daemon 线程也不会把解释器退出拖住。
    serviceThread = threading.Thread(
        target=runService, name="daqmt-quote-service", daemon=True,
    )
    serviceThread.start()
    while running and not serviceDone.wait(timeout=0.1):
        pass
    if running:
        return exitCode[0]

    started = time.monotonic()
    logger.info("SHUTDOWN_BEGIN timeout_s=%.1f", SHUTDOWN_TIMEOUT_SEC)
    if serviceDone.wait(timeout=SHUTDOWN_TIMEOUT_SEC):
        logger.info(
            "SHUTDOWN_COMPLETE elapsed_s=%.3f service_exit_code=%d",
            time.monotonic() - started, exitCode[0],
        )
    else:
        logger.warning(
            "SHUTDOWN_TIMEOUT elapsed_s=%.3f 服务清理未完成，结束本地进程；远端退订状态未确认",
            time.monotonic() - started,
        )
    # 人工停止本地进程是正常退出；远端清理失败/超时由独立日志明确披露。
    return 0


def _runQuoteService(logger: logging.Logger) -> int:
    lockPath = defaultInstanceLockPath()
    lockFile = acquireInstanceLockOrSteal(lockPath, logger)
    if lockFile is None:
        logger.error("无法获取实例锁 %s（旧实例终止后仍占用）", lockPath)
        return 1
    logger.info("实例锁已占用 %s pid=%s", lockPath, os.getpid())

    _ensureXtquantSrc()
    from bigqmt_signal_trader.quote_universe import build_quote_universe, reject_market_codes
    from bigqmt_signal_trader.xtquant_compat import configure, xt_trader, xtdata

    logger.info("configure() … 不要并行 quote-live 订同一 7214 combo")
    configure()
    uni = build_quote_universe(xtdata)
    codes = uni["codes"]
    reject_market_codes(codes)
    if not codes:
        logger.error("全推宇宙为空")
        releaseInstanceLock(lockFile)
        return 1
    logger.info(
        "宇宙 hs_a=%s bj=%s etf=%s total=%s sectors=%s",
        uni["counts"].get("hs_a"),
        uni["counts"].get("bj"),
        uni["counts"].get("etf"),
        uni["counts"].get("total"),
        uni["sectors"],
    )

    httpServer = startQuoteHttp(logger)
    if httpServer is None:
        stealPreviousQuoteInstance(logger, lockPath)
        httpServer = startQuoteHttp(logger)
    if httpServer is None:
        logger.error("15580 被占用，拒绝双开")
        releaseInstanceLock(lockFile)
        return 1
    peers = scanQuoteLivePids()
    if peers:
        logger.error("QUOTE_PEER_CONFLICT quote-live pid=%s 会抢 7214 combo", peers)
    subIdBox = {"v": None}
    optThread = None
    metaThread = None
    tdxFallbackStop = threading.Event()
    tdxFallbackThread = None
    tdxMergeStop = threading.Event()
    tdxMergeThread = None
    try:
        # QMT 策略桥接未启动时也先把 TDX-only 路径拉起。旧流程在下面的
        # waitUntilRpcPing() 阻塞，导致 15581 已健康却 15580 全空。
        fallbackCodes = prepareTdxFallbackChain(logger)
        if fallbackCodes:
            tdxFallbackThread = threading.Thread(
                target=_restartableDaemonLoop,
                args=(logger, "TDX_FALLBACK_LOOP", _runTdxFallbackLoop, logger, tdxFallbackStop),
                name="daqmt-tdx-fallback",
                daemon=True,
            )
            tdxFallbackThread.start()
        if not waitUntilRpcPing(logger, xt_trader):
            logger.error("等待桥接 RPC 期间收到退出信号")
            return 1
        # QMT 已恢复后先让 TDX-only 线程退出，再进入常规的 QMT 主供 + TDX 合并。
        tdxFallbackStop.set()
        if tdxFallbackThread is not None:
            tdxFallbackThread.join(timeout=TDX_OPT_FETCH_TIMEOUT_SEC + 1.0)
        if TDX_OPT_ENABLE and TDX_OPT_DEDICATED_MERGE:
            tdxMergeThread = threading.Thread(
                target=_restartableDaemonLoop,
                args=(
                    logger,
                    "TDX_MERGE_LOOP",
                    _runTdxMergeLoop,
                    logger,
                    tdxMergeStop,
                ),
                name="daqmt-tdx-merge",
                daemon=True,
            )
            tdxMergeThread.start()
            logger.info(
                "TDX_OPT dedicated merge start interval_s=%.2f",
                TDX_OPT_MERGE_INTERVAL_SEC,
            )
        subIdBox["v"] = subscribeWholeQuoteUntilOk(logger, xtdata, codes)
        if subIdBox["v"] is None:
            logger.error("全推订阅在退出前仍未成功")
            return 1

        optCodes, optSource = resolveEtfOptionCodes(
            logger, xt_trader, xtdata, skipPing=True,
        )
        # skipPing 路径刻意只取三条 QMT 主链，TDX fallback 则在等待 RPC
        # 之前准备了五链。进入轮询前必须合并两者；否则重启后会将
        # 510050/159915 从 OI/TDX 调度宇宙中丢掉（典型日志 szo=0）。
        mergedOptCodes = mergeOptionUniverseCodes(optCodes, fallbackCodes)
        if fallbackCodes and len(mergedOptCodes) != len(optCodes or []):
            logger.info(
                "期权宇宙 merge qmt_n=%d tdx_fallback_n=%d total=%d",
                len(optCodes or []),
                len(fallbackCodes),
                len(mergedOptCodes),
            )
            optSource = "%s+tdx_fallback" % optSource
        optCodes = mergedOptCodes
        if optCodes:
            logger.info("期权宇宙 source=%s n=%d", optSource, len(optCodes))
            reloadAdjustedOptionCodes(logger)
            seedOptionMetaFromOptBasic(logger)
            _seedListedTodayFromOptBasic(logger, date.today())
        else:
            logger.error("期权宇宙为空 source=%s，股票全推仍继续", optSource)
        if optCodes:
            _setOptionChainCodes(optCodes)
            optThread = threading.Thread(
                target=_restartableDaemonLoop,
                args=(logger, "OPT_POLL_LOOP", _runOptionPollLoop, logger, xtdata, optCodes),
                name="daqmt-opt-poll",
                daemon=True,
            )
            optThread.start()
            metaThread = threading.Thread(
                target=_restartableDaemonLoop,
                args=(logger, "OPT_META_LOOP", _runOptionMetaFillLoop, logger, xtdata),
                name="daqmt-opt-meta",
                daemon=True,
            )
            metaThread.start()

        _restartableDaemonLoop(
            logger, "WATCHDOG_LOOP", _runWatchdogLoop,
            logger, xt_trader, xtdata, codes, subIdBox,
        )
    finally:
        cleanupOk = True
        tdxFallbackStop.set()
        tdxMergeStop.set()
        if tdxFallbackThread is not None:
            cleanupOk = _runShutdownStep(logger, "tdx_fallback", lambda: _joinQuoteWorker(
                tdxFallbackThread, TDX_OPT_FETCH_TIMEOUT_SEC + 1.0,
            )) and cleanupOk
        if tdxMergeThread is not None:
            cleanupOk = _runShutdownStep(logger, "tdx_merge", lambda: _joinQuoteWorker(
                tdxMergeThread, TDX_OPT_FETCH_TIMEOUT_SEC + 1.0,
            )) and cleanupOk
        oldId = subIdBox.get("v")
        if oldId is not None:
            def unsubscribe() -> None:
                xtdata.unsubscribe_quote(oldId)
                logger.info("已退订 sub_id=%s", oldId)
            cleanupOk = _runShutdownStep(logger, "unsubscribe", unsubscribe) and cleanupOk
        if optThread is not None:
            cleanupOk = _runShutdownStep(logger, "option_worker", lambda: _joinQuoteWorker(
                optThread, 2.0,
            )) and cleanupOk
        if metaThread is not None:
            cleanupOk = _runShutdownStep(logger, "meta_worker", lambda: _joinQuoteWorker(
                metaThread, 1.0,
            )) and cleanupOk
        if httpServer is not None:
            cleanupOk = _runShutdownStep(logger, "http", lambda: _stopQuoteHttp(httpServer)) and cleanupOk
        session = getattr(xtdata, "_quote_session", None)
        if session is not None:
            cleanupOk = _runShutdownStep(logger, "quote_session", session.stop) and cleanupOk
        logger.info("退出 %s", _heartbeatLine())
        cleanupOk = _runShutdownStep(logger, "instance_lock", lambda: releaseInstanceLock(lockFile)) and cleanupOk
    return 0 if cleanupOk else 1


if __name__ == "__main__":
    sys.exit(main())

# 代码更新时间：2026-08-24 20:38
# 代码更新时间：2026-08-25 06:32 /snapshot 保留 slim 决策字段，不再裁成 6 键
# 代码更新时间：2026-08-25 14:52 期权快照不中价补 last，丢掉非当日 last
# 代码更新时间：2026-08-25 15:19 last 被滤掉仍保留 vol/oi 供 T 型叠量
# 代码更新时间：2026-08-25 17:36 废 tick 丢量；日K 覆盖 last/vol/oi 对齐 T 型板
# 代码更新时间：2026-08-25 17:55 SZO 优先灌 tick 库；盘后仍轮询；废 tick 清盘口
# 代码更新时间：2026-08-25 18:18 盘后用 ETF 日K 填现货 last/OHLC
# 代码更新时间：2026-08-25 18:27 QMT 占位日K 回退 rt_k；先刷现货再轮询期权
# 代码更新时间：2026-08-25 18:47 盘中保留更新 tick；禁灌库；日K 节流；快照短锁
# 代码更新时间：2026-08-25 19:11 盘中先 tick 后日K；有量合约优先；轮询间隔缩短
# 代码更新时间：2026-08-25 19:18 期权链按日切/10分钟重拉，分片随张数变化
# 代码更新时间：2026-08-25 19:23 期权链改为仅日切重拉，盘中不再定时拉链
# 代码更新时间：2026-08-25 19:34 后台补合约要素 optMeta，供 T 型新挂牌
# 代码更新时间：2026-08-25 20:41 日切拉链；标准优先轮询；快照 JSON 0.2s；ping 12s
# 代码更新时间：2026-08-26 06:17 opt_basic 回退含摘牌当日
# 代码更新时间：2026-08-26 08:45 启动时终止旧实例再占锁，确保单实例
# 代码更新时间：2026-08-26 08:46 抢锁只杀 python.exe，不杀 bash 包装进程
# 代码更新时间：2026-08-26 09:10 启动等 RPC、订全推失败重试
# 代码更新时间：2026-08-26 09:23 盘中停 META；RPC 串行；optMeta 预填；片失败对半重试
# 代码更新时间：2026-08-26 09:47 盘中灌当日 tick；按标的整链轮询量仓
# 代码更新时间：2026-08-26 09:53 盘中量仓改 15s 日K；tick 灌库仅盘前/午休
# 代码更新时间：2026-08-26 09:58 盘中跳过废日 tick，先刷 1d 量仓
# 代码更新时间：2026-08-26 10:08 588000 日K 热刷；其余链 12s
# 代码更新时间：2026-08-26 10:15 五条期权链同等刷新 1d
# 代码更新时间：2026-08-26 10:28 剔除 1d 占位柱；SZO 补灌当日日K
# 代码更新时间：2026-08-26 12:09 午休并入直播窗，tick 仅盘前/15:31后灌
# 代码更新时间：2026-08-26 12:11 直播窗内补刷 5 只 ETF 日K，午休冷启动有现货
# 代码更新时间：2026-08-26 13:37 创业板 1d 定期灌库，解开上午冻结柱
# 代码更新时间：2026-08-26 14:50 沪市 1d 片 200；创业板 subscribe_quote 1d 每日确认
# 代码更新时间：2026-08-26 15:41 订阅灌码期只刷沪市日K、禁 stub 灌库；收盘后不轮询 tick
# 代码更新时间：2026-08-26 22:40 日K闸门不可绕过：订未满 sho_only；订满才读 SZO；订阅挂才灌库
# 代码更新时间：2026-08-27 07:32 直播窗 ETF 现货改全推；片间让出 0.05s
# 代码更新时间：2026-08-27 10:51 启动延迟拉链；直播窗窄口 META；ProductType 认购认沽
# 代码更新时间：2026-08-27 13:57 直播窗只灌拉链新挂；禁止空缓存整链灌 tick 饿死 1d
# 代码更新时间：2026-08-27 14:37 盘中 1d 盖过冻结 tick；不再轮询 get_option_ticks
# 代码更新时间：2026-08-27 14:57 今日新挂定期灌 1d；opt_basic 当日上市并入新挂名单
# 代码更新时间：2026-08-27 17:56 拉链未增长重试；SZO 订未满仍刷已订/新挂；近3日灌1d；META 15:31接上
# 代码更新时间：2026-08-27 19:34 新挂灌库改交易日窗口；直播窗不再按 tick 缺口睡
# 代码更新时间：2026-08-28 06:57 直播窗先 1d 再新挂/META；LIVE=1s；快照 JSON 1s
# 代码更新时间：2026-08-28 07:08 片 280；META 直播 8 张；IDLE 10s；去掉 1s 空等
# 代码更新时间：2026-08-28 07:22 直播窗 get_option_ticks 主源；冻 tick 回退 1d；沪深订 1d
# 代码更新时间：2026-08-28 10:06 假活不在 ROUTER 忙时真退真订；片 50；round 后歇
# 代码更新时间：2026-08-28 10:20 1d 订阅先 SZO；闸门只等创业板订满
# 代码更新时间：2026-08-28 10:25 subscribe_quote 只订 SZO，沪市不占 500 配额
# 代码更新时间：2026-08-28 10:35 SZO 改订 tick；同日 tick 量不低于 1d 不盖掉
# 代码更新时间：2026-08-28 11:04 订 tick 实测无效；盘中灌当日 SZO tick；1d 拉稀
# 代码更新时间：2026-08-28 11:20 创业板改 1m 灌库合成量；get_option_ticks 只拉沪市
# 代码更新时间：2026-08-28 13:45 订阅空转当日放弃；1m 按分钟柱过期
# 代码更新时间：2026-08-28 15:07 实测 SZO 全推/订阅均 0 回调，配额全给沪市
# 代码更新时间：2026-08-28 16:00 1m/1d 取回改本地 datadir 直读，缺口才回退 RPC
# 代码更新时间：2026-08-31 10:11 ping 6s；tick 轮转含 SZO；缓存空重订；1m 不盖盘口
# 代码更新时间：2026-08-31 10:51 有档不灌1m；加订仍拉SZO；新挂10倍日线不盖last
# 代码更新时间：2026-08-31 11:23 创业板 1m/快照丢弃 10 倍权利金
# 代码更新时间：2026-08-31 14:53 全推 force 拆死 combo；期权让路；ETF 探针；禁伪 lastPush
# 代码更新时间：2026-08-31 15:30 沪市四链交错订阅；tick 片不再整链被 50ETF 占满
# 代码更新时间：2026-08-31 15:55 后盘 catch-up 回捞 tick/缓存；10 倍权利金缩 /10
# 代码更新时间：2026-08-31 17:38 科创+500+300 全订、50ETF 全轮询；静默/BUSY 让路；直播窗 1 片
# 代码更新时间：2026-08-31 20:34 创业板直播窗只读本地 1m，不 download、不打五档
# 代码更新时间：2026-09-01 07:58 单实例互斥强化；空订当失败；静默清空假 unique
# 代码更新时间：2026-09-01 10:16 空缓存不拆订阅；全推健康后小片灌今日 tick/1m；空 last 不覆盖
# 代码更新时间：2026-09-01 10:19 灌库后立刻 get_option_ticks；直播窗 40 张/轮 + 4 片轮询
# 代码更新时间：2026-09-01 10:21 全推健康时加订不再跳过灌库；灌库后按 20 张回读
# 代码更新时间：2026-09-01 10:47 过期盘口小片增量灌库；相同 tick 不再刷 opt_last
# 代码更新时间：2026-09-01 10:53 过期刷新按标的交错轮转，避免只刷 50ETF
# 代码更新时间：2026-09-01 11:05 全推站住时过期刷新 40 张/轮，否则 20
# 代码更新时间：2026-09-01 11:35 过期刷新有量优先+轮转扫尾；SZO 1m 约 30s 一灌
# 代码更新时间：2026-09-02 09:55 SHO/SZO 本地 DAT 按 4 位小数读，避免虚值认沽 last×10
# 代码更新时间：2026-09-02 12:47 订满空缓存 force 分片重订；定期 heal sync 交给桥接自愈
# 代码更新时间：2026-09-02 12:57 午休禁止 force；连续竞价冷却 90s，避免空缓存死循环拆订
# 代码更新时间：2026-09-03 07:21 启动校桥接 revision；订满空缓存打 OPT_HEALTH dead_push
# 代码更新时间：2026-09-03 10:06 半残订阅只重订未缓存句柄；盘前未订满不灌整链
# 代码更新时间：2026-09-03 10:34 同一批 heal 3 轮无效改 poll-only，停止空退空订
# 代码更新时间：2026-09-03 11:39 拉链新增立刻补 optMeta；直播窗窄口 8 张真正接上
# 代码更新时间：2026-09-07 10:18 159915/510050 通达信独立喂数合并，QMT 兜底
# 代码更新时间：2026-09-09 15:46 OPT_CHUNK 超时对半(≥8)+压力期缩小分片，减轻午间 get_market_data_ex 堵
# 代码更新时间：2026-09-09 16:49 TDX 合并扩至五链（UNDLS 可 env）；三链慢刷兜底
# 代码更新时间：2026-09-09 17:08 盘后仍合并 TDX（去掉 inWindow 门闩），供收盘盘口
# 代码更新时间：2026-09-09 18:50 TDX拉取超时默认2s；快照保留fromTdx；未变盘口也补标记
# 代码更新时间：2026-09-10 09:27 空TDX不落库/不锁fresh；TDX覆盖链跳过OPT_CHUNK
# 代码更新时间：2026-09-10 09:39 ETF日K显式OHLCV；日志轮转失败不抛到业务线程
# 代码更新时间：2026-09-07 17:47 tick 超时退避 30s；WARNING 60s 限频；空订 30s 退避
# 代码更新时间：2026-09-08 15:11 _sleepInterruptible 防 sleep 负数竞态；改用 monotonic
# 代码更新时间：2026-09-09 06:54 期权/看门狗守护循环异常可重启，避免静默挂掉
# 代码更新时间：2026-09-10 10:51 TDX缺oi保留/QMT补量仓；指纹含oi
# 代码更新时间：2026-09-10 10:56 默认不跳TDX覆盖链OPT_CHUNK，FillQty补QMT量/仓
# 代码更新时间：2026-09-10 11:02 每轮 OPT_QTY 轮询补 SHO 量仓（约 80 张/轮）
# 代码更新时间：2026-09-10 11:08 OPT_YIELD前仍做TDX+OPT_QTY；缺oi优先；失败不跳rotate
# 代码更新时间：2026-09-10 11:16 SUBSCRIBE_NOOP 90s 放弃以免永久挡期权
# 代码更新时间：2026-09-10 11:22 OPT_QTY 片5/超时18s（实测10张约11s）
# 代码更新时间：2026-09-10 11:26 MergeKeepQty 量取max，防TDX滞后Volume回盖
# 代码更新时间：2026-09-10 11:33 QMT纠偏量仓/最新；盘口指纹忽略纯时间跳动
# 代码更新时间：2026-09-10 13:00 FillQty last独立更新；YIELD中仍跑OPT_QTY；快照保留qtyFromQmt
# 代码更新时间：2026-09-10 13:07 OPT_QTY每轮预留高成交量hot席位
# 代码更新时间：2026-09-10 13:17 可用fromTdx禁止QMT整票替换，只走FillQty防量仓回盖
# 代码更新时间：2026-09-10 14:05 OPT_QTY 宇宙含 SZO（get_option_ticks 补创业板持仓）
# 代码更新时间：2026-09-15 18:55 盘后停QMT补齐；只保留不占RPC的TDX本地快照合并
# 代码更新时间：2026-10-01 08:04 主线程独立处理信号、总退出等待5秒、分阶段清理与HTTP端口关闭
# 代码更新时间：2026-10-05 09:52 密钥改读环境变量，去掉默认口令
