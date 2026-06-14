# ETF 期权垂直价差回测 — 实施计划

> **For Claude/执行者：** 配套设计见 `docs/plans/2026-06-14-opt-vertical-spread-backtest-design.md`。逐任务实现，每任务末尾提交。

**Goal:** 用历史数据回测四个 ETF 期权的牛市认购/熊市认沽垂直价差，输出"初始成本区间、止盈倍数、到期月选择"三大结论。

**Architecture:** 单文件脚本 `opt_vertical_spread_backtest.py`。一次性把每个标的的 `opt_basic`/`opt_daily`/`etf_daily` 读入内存为 DataFrame，纯函数完成（到期月分类→选腿配对→单笔回测→统计），主循环按"方案A条件回测 + 方案B信号回测"两路跑网格，结果落 CSV + 控制台摘要。

**Tech Stack:** Python 3.12、pandas、numpy、SQLAlchemy(+pymysql)、MySQL `stock_tushare`。遵循仓库 camelCase、同名日志、可中断、Windows 安全日志轮转约定。

---

## 约定

- 文件：`opt_vertical_spread_backtest.py`（仓库根目录）
- 日志：`opt_vertical_spread_backtest.log`（同名、TimedRotating、backupCount=3、Windows 安全）
- 单测：`tests/test_opt_vertical_spread.py`（仅覆盖纯函数；运行 `python -m pytest tests/test_opt_vertical_spread.py -v`）
- 输出：`out/opt_spread_*.csv`
- DB 连接、TUSHARE 无关（本回测不调 Tushare，仅读本地库），DB_CONFIG 与 `opt_daily.py` 一致（env 可覆盖，默认 root/123456/localhost/stock_tushare）

---

## Task 1: 脚本骨架（配置、日志、DB、参数、可中断）

**Files:**
- Create: `opt_vertical_spread_backtest.py`

**Step 1: 写骨架**

包含：
- 文件头 docstring（接口说明、运行示例）
- `WindowsSafeTimedRotatingFileHandler` + `setupLogger()`（直接复用 `opt_daily.py` 同款实现）
- `running` 全局 + `handleSignal` + SIGINT/SIGTERM 注册
- `DB_CONFIG`（env 覆盖）、`makeEngine()`（复用 `opt_daily.py` 写法）
- 配置常量区：
  ```python
  TARGETS = ["510300.SH", "510500.SH", "588000.SH", "159915.SZ"]
  PER_UNIT = 10000
  TICK = 0.0001
  SLIP_TICKS = 1            # 每腿每边滑点档数
  FEE_PER_LEG = 2.0         # 每腿每张手续费(元)，可设0
  MIN_OI = 50               # 建仓流动性阈值
  WIDTHS = (1, 2, 3)        # 价差宽度(档)
  TP_MULTS = (1.3, 1.5, 1.8, 2.0, 2.5)  # 止盈倍数
  SL_MULT = 0.5             # 止损倍数(可选, None 关闭)
  EXIT_BEFORE_EXPIRE_DAYS = 1
  SAMPLE_FREQ = "W-MON"     # 方案A采样: 每周一; 可改 "D"
  EXPIRY_KINDS = ("near", "next", "quarter", "next_quarter")
  ```
- `argparse`：`--codes`（默认全部）、`--freq`、`--price`(close/settle)、`--mode`(A/B/both)、`--out`

**Step 2: smoke 验证**

Run: `python opt_vertical_spread_backtest.py --help`
Expected: 打印参数帮助，无异常。

**Step 3: 提交**

```bash
git add opt_vertical_spread_backtest.py
git commit -m "feat(opt-spread): 回测脚本骨架(配置/日志/DB/可中断)"
```

---

## Task 2: 数据加载层

**Files:**
- Modify: `opt_vertical_spread_backtest.py`

**Step 1: 实现 `loadOptMeta(engine, target)`**

读该标的全部合约：`SELECT ts_code, call_put, exercise_price, maturity_date, list_date, delist_date FROM opt_basic WHERE opt_code = :oc`，`oc = 'OP' + target`。返回 DataFrame，`maturity_date`/`list_date`/`delist_date` 转 `datetime`，`exercise_price` 转 float。

**Step 2: 实现 `loadOptDaily(engine, ts_codes)`** 

按 ts_code 批量读 `opt_daily`（`ts_code, trade_date, close, settle, vol, oi`），`trade_date` 转 datetime。返回以 `(ts_code, trade_date)` 可索引的结构（建议 set_index 或 pivot 成 `{ts_code: Series}` 的 close/settle/vol/oi 查找表）。

**Step 3: 实现 `loadEtfDaily(engine, target)`**

读 `etf_daily`（`trade_date, close, close_qfq, ma20`），转 datetime，按 trade_date 排序。

**Step 4: smoke 验证**

临时在 `main` 里对 510300 调用三个加载函数并 `logger.info` 行数与日期范围。
Run: `python opt_vertical_spread_backtest.py --codes 510300.SH`
Expected: 日志显示 opt_basic ~2822、etf_daily 起止合理、无异常。验证后移除临时打印。

**Step 5: 提交**

```bash
git add opt_vertical_spread_backtest.py
git commit -m "feat(opt-spread): 数据加载层(opt_basic/opt_daily/etf_daily)"
```

---

## Task 3: 到期月分类（纯函数 + 单测）

**Files:**
- Modify: `opt_vertical_spread_backtest.py`
- Create: `tests/test_opt_vertical_spread.py`

**Step 1: 写失败测试**

```python
import datetime as dt
import pandas as pd
from opt_vertical_spread_backtest import classifyExpiries

def test_classify_expiries_basic():
    # 模拟交易日 2024-01-15, 可交易到期: 1月(当月,普通)、2月(下月)、3月(季)、6月(下季)
    mats = [dt.date(2024,1,24), dt.date(2024,2,28), dt.date(2024,3,27), dt.date(2024,6,26)]
    t = dt.date(2024,1,15)
    out = classifyExpiries(t, mats)
    assert out["near"] == dt.date(2024,1,24)
    assert out["next"] == dt.date(2024,2,28)
    assert out["quarter"] == dt.date(2024,3,27)
    assert out["next_quarter"] == dt.date(2024,6,26)
```

**Step 2: 运行确认失败**

Run: `python -m pytest tests/test_opt_vertical_spread.py -v`
Expected: FAIL（`classifyExpiries` 未定义）

**Step 3: 实现 `classifyExpiries(t, maturities)`**

规则：入参为某交易日 t 与该日"未到期且在交易"的去重升序到期日列表。
- `near` = 列表第 0 个；`next` = 第 1 个（不足则 None）。
- 季月 = 月份 ∈ {3,6,9,12} 的到期日升序；`quarter` = 第 0 个，`next_quarter` = 第 1 个。
- 允许 near 与 quarter 重叠（如 t 在 3 月，当月即季月），返回时各键独立取值。

**Step 4: 运行确认通过**

Run: `python -m pytest tests/test_opt_vertical_spread.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add opt_vertical_spread_backtest.py tests/test_opt_vertical_spread.py
git commit -m "feat(opt-spread): 到期月分类(当月/下月/当季/下季)+单测"
```

---

## Task 4: 选腿与配对（纯函数 + 单测）

**Files:**
- Modify: `opt_vertical_spread_backtest.py`
- Modify: `tests/test_opt_vertical_spread.py`

**Step 1: 写失败测试**

构造一个小的 chain DataFrame（同一 maturity、同 call_put、若干行权价及当日 close/vol/oi），断言：
- `pickLegs(chain, spot, call_put="C", width=1, side="bull")` 返回买腿为最接近 spot 的 K，卖腿为高一档 K。
- `call_put="P", side="bear", width=2` 返回买腿最接近平值、卖腿低两档。
- 流动性不足（oi < MIN_OI）或档位不够时返回 None。

**Step 2: 运行确认失败**

Run: `python -m pytest tests/test_opt_vertical_spread.py::test_pick_legs -v`
Expected: FAIL

**Step 3: 实现 `pickLegs(chain, spot, call_put, width, side)`**

- chain 按 `exercise_price` 升序；找 `|K - spot|` 最小的索引 i 作为平值买腿（bull-call / bear-put 的买腿都取近平值）。
- bull（认购牛市）：买 i，卖 i+width（更高 K）。
- bear（认沽熊市）：买 i，卖 i−width（更低 K）。
- 校验：买卖腿存在、当日有 close、`oi ≥ MIN_OI`、`vol > 0`；否则 None。
- 返回 dict：买/卖腿的 ts_code、K、建仓 close。

**Step 4: 运行确认通过 + 提交**

Run: `python -m pytest tests/test_opt_vertical_spread.py -v` → PASS
```bash
git add opt_vertical_spread_backtest.py tests/test_opt_vertical_spread.py
git commit -m "feat(opt-spread): 选腿配对(近平值买腿+W档卖腿)+单测"
```

---

## Task 5: 单笔回测（纯函数 + 单测）

**Files:**
- Modify: `opt_vertical_spread_backtest.py`
- Modify: `tests/test_opt_vertical_spread.py`

**Step 1: 写失败测试**

给定建仓净借记、一段逐日价差现值序列、止盈倍数 K、止损、到期日，断言 `simulateTrade(...)`：
- 价差先触及 K 倍 → 以止盈价差平仓，盈亏 = (止盈价差−建仓)×PER_UNIT − 成本。
- 一路下行触止损 → 止损平。
- 既不止盈也不止损 → 到期前 D 天按当时价差平。
- 返回 dict 含 `pnl`、`ret`(对净借记 %)、`hold_days`、`exit_reason`。

**Step 2: 运行确认失败 → Step 3: 实现 `simulateTrade(entryDebit, spreadSeries, dates, kTp, kSl, exitDate)`**

- 净价差现值 = 买腿 close − 卖腿 close（同日）。
- 滑点/手续费：建仓与平仓各计 `2 腿 × (SLIP_TICKS×TICK×PER_UNIT) + 2×FEE_PER_LEG`。
- 逐日：若 spread ≥ entryDebit×kTp → 止盈平；elif kSl 且 spread ≤ entryDebit×kSl → 止损平；到 exitDate 强平。
- pnl 含全部成本；ret = pnl / (entryDebit×PER_UNIT)。

**Step 4: 通过 + 提交**

```bash
git commit -am "feat(opt-spread): 单笔回测(止盈/止损/到期平仓+成本)+单测"
```

---

## Task 6: 方案 A 条件回测主循环

**Files:**
- Modify: `opt_vertical_spread_backtest.py`

**Step 1: 实现 `runConditionalBacktest(data, target, ...)`**

- 采样日 = `etf_daily` 交易日按 `SAMPLE_FREQ` 取（周一或日频）。
- 每个采样日 t：spot = 当日 ETF close；对 `EXPIRY_KINDS` 各取 maturity（Task3）；对每个 maturity 取 chain（同 call_put：bull→C / bear→P）；对 `WIDTHS` 选腿（Task4）；构造逐日价差序列至 `exitDate=maturity − EXIT_BEFORE_EXPIRE_DAYS`；对 `TP_MULTS` 各跑 `simulateTrade`（Task5）。
- 同时记录"方向是否正确"：bull 看 `spot(exit) > spot(entry)`，bear 看 `<`，用于条件子样本。
- 每条结果 append 到列表：标的、方向、到期类别、宽度、止盈倍数、初始成本相对值、pnl、ret、hold_days、win、dir_correct、exit_reason。

**Step 2: smoke 验证**

Run: `python opt_vertical_spread_backtest.py --codes 510300.SH --mode A`
Expected: 日志显示生成 N 条交易（数千量级），无异常，耗时可接受。

**Step 3: 提交**

```bash
git commit -am "feat(opt-spread): 方案A条件回测主循环(四到期月×宽度×止盈网格)"
```

---

## Task 7: 方案 B 均线信号回测

**Files:**
- Modify: `opt_vertical_spread_backtest.py`

**Step 1: 实现 `runSignalBacktest(data, target, ...)`**

- 信号：ETF `close` 上穿 `ma20` → 当日建牛市认购价差；下穿 → 建熊市认沽价差。
- 同一时刻仅持一笔（信号反向或平仓后才再开），到期类别默认 `near`+`next` 两版各跑。
- 复用 Task4/Task5；记录净值序列（按笔串联）。

**Step 2: smoke 验证** `--mode B`，确认产出交易与净值无异常。

**Step 3: 提交**

```bash
git commit -am "feat(opt-spread): 方案B均线信号回测+净值"
```

---

## Task 8: 统计聚合与输出

**Files:**
- Modify: `opt_vertical_spread_backtest.py`

**Step 1: 实现 `summarize(trades_df)`**

按 (标的 × 方向 × 到期类别 × 宽度 × 止盈倍数) groupby，计算：样本数、可成交率、胜率、平均 ret、盈亏比、`期望ret/平均hold_days`、最大回撤、最大单笔亏损；条件子样本（dir_correct=True）单列一份。

**Step 2: 实现 `exportAndConclude(...)`**

- 落 CSV：`out/opt_spread_grid_{ts}.csv`、`out/opt_spread_conditional_{ts}.csv`。
- 控制台打印结论三段：① 初始成本相对值最优区间（按时间效率分桶）；② 各到期类别下最优止盈倍数；③ 跨到期类别推荐。
- 输出用纯文本标识（避免 ✓✗ 等，遵循 PowerShell 编码规则）。

**Step 3: smoke 验证** 全量 `python opt_vertical_spread_backtest.py`（四标的、A+B），确认 CSV 生成、摘要打印。

**Step 4: 提交**

```bash
git add opt_vertical_spread_backtest.py
git commit -m "feat(opt-spread): 统计聚合+CSV输出+结论摘要"
```

---

## Task 9: 全量跑批、敏感性与结论落档

**Files:**
- Create: `docs/plans/2026-06-14-opt-vertical-spread-RESULTS.md`

**Step 1:** 全量跑 `--price close`，再跑 `--price settle` 做敏感性对照。
**Step 2:** 把三大结论（成本区间 / 止盈倍数 / 到期月）与关键网格表写入 RESULTS.md，标注样本量与稳健性（按标的、按方向是否一致）。
**Step 3:** 末尾时间戳，提交。

```bash
git add docs/plans/2026-06-14-opt-vertical-spread-RESULTS.md
git commit -m "docs(opt-spread): 回测结论与敏感性结果"
```

---

## 代码质量检查清单（执行中持续遵守）

- camelCase 命名；纯函数无副作用、便于单测。
- 每个 py 文件末尾 `# 代码更新时间：YYYY-MM-DD HH:MM`（用本机时间，禁止猜）。
- 日期统一 `pd.to_datetime`，内部用 datetime 比较。
- 长循环可被 `running` 中断；大查询按标的分批读，避免一次性 2000 万行。
- 完成后 `ReadLints` 检查。
