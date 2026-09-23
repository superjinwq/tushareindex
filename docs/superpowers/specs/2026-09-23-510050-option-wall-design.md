# 510050 独立期权墙：第一阶段 OI Wall 设计

**日期：** 2026-09-23

**状态：** 待用户书面审阅

**对象：** 510050 上证 50 ETF 期权

**阶段：** 第一项，仅实现 EOD OI Wall 与计算过程教学

## 1. 目标

新建一套与现有 `rt-monitor/opt-t-board` 完全隔离的 510050 期权墙脚本和页面。第一阶段只回答三个问题：

1. 最新完整交易日的 Call 和 Put 持仓主要集中在哪些行权价？
2. 页面上的墙位由哪些原始合约、哪些聚合步骤计算得到？
3. 当前数据是否足以支持该结论，是否存在缺失或调整合约？

页面用于结构观察和学习，不输出买卖建议，也不把 Call Wall、Put Wall直接定义为阻力或支撑。

## 2. 成功标准

- 独立脚本可在 Windows 11、Python 3.12 下启动，默认仅监听 `127.0.0.1`。
- 独立页面不依赖现有 T 型板路由、模板和前端状态。
- 只读查询 `stock_tushare`，不建表、不写库、不修改源数据。
- 页面显示同一个 `dataDate` 下的期权 OI 与 ETF 现货价格，禁止跨日拼接。
- 展示最近四个有数据的未到期期限；每个期限分别计算 Call、Put OI Wall。
- 用户可以从墙位结果向下追溯到行权价聚合和原始合约贡献。
- 缺失 OI、调整合约、日期错配、空期限都有明确状态，不静默补零。
- 计算核心具有脱离 Flask 和数据库的纯函数测试。

## 3. 本阶段范围

### 包含

- 最新完整交易日识别。
- 510050 ETF 同日 `close_qfq` 现货价格。
- 最近四个未到期期限。
- 标准合约 Call/Put OI 按行权价聚合。
- Call OI Wall、Put OI Wall、Top 3 集中行权价。
- 墙位距现货百分比。
- 原始 OI、聚合过程、排序过程和数据质量说明。
- 独立 HTML 页面和只读 JSON API。

### 不包含

- 盘中实时行情或自动轮询。
- 1D/3D/5D Flow、速度、加速度、迁移和换月识别。
- Max Pain。
- IV、Delta、Gamma、Gamma Wall。
- Estimated Dealer GEX、Gamma Flip。
- 交易信号、方向预测、策略建议和历史回测。
- 与现有 `rt-monitor/opt-t-board` 的融合。

## 4. 方案选择

采用独立 Flask 脚本，而不是现有应用 Blueprint，也不生成一次性的静态 HTML。

### 文件边界

- `opt_wall_510050.py`
  - 独立 Flask 入口。
  - 只读数据库查询。
  - 数据清洗、聚合和 JSON 组装。
  - 暴露页面、数据和健康检查接口。
- `templates/opt_wall_510050.html`
  - 单页教学界面。
  - 使用原生 HTML/CSS/JavaScript，不新增前端依赖。
- `tests/test_opt_wall_510050.py`
  - 纯函数、数据日期、调整合约、并列墙位和 Flask 路由测试。

第一阶段保持三文件结构；只有当后续 Flow/Gamma 导致 Python 文件职责明显过载时，才拆分 `services`，不预先建立复杂包结构。

### 运行方式

```powershell
python opt_wall_510050.py
```

- 默认地址：`http://127.0.0.1:5011/`
- 环境变量：`OPT_WALL_HOST`、`OPT_WALL_PORT`
- 默认关闭 Flask debug 和自动重载，避免重复进程。

## 5. 数据来源和共同数据日

### 数据表

- `stock_tushare.opt_basic`
  - `ts_code`、`opt_code`、`call_put`、`exercise_price`、`maturity_date`、`list_date`、`delist_date`、`per_unit`。
- `stock_tushare.opt_daily`
  - `ts_code`、`trade_date`、`oi`、`vol`、`close`、`settle`。
- `stock_tushare.etf_daily`
  - `ts_code='510050.SH'`、`trade_date`、`close_qfq`。

### 合约范围

- `opt_basic.opt_code='OP510050.SH'`。
- `call_put IN ('C', 'P')`。
- 合约在 `dataDate` 当日已经上市且尚未退市。
- 只选择在 `opt_daily` 的 `dataDate` 有记录的合约。

### `dataDate` 选择

`dataDate` 必须同时满足：

1. 510050 期权链在 `opt_daily` 有数据；
2. `etf_daily` 的 `510050.SH` 有同日 `close_qfq`；
3. 至少存在一个未到期期限。

从满足条件的日期中取最大值。禁止使用“期权最新日 + ETF 另一个最新日”的拼接方式。如果找不到共同日期，接口返回明确错误，不退回跨日数据。

## 6. 标准合约与调整合约

510050 标准合约的 `per_unit` 通常为 10,000。历史上可能存在权益调整后的 A 类合约，其乘数和行权价不能与标准合约无提示混合。

第一阶段采用以下规则：

- `per_unit=10000`：进入主 OI Wall 计算。
- `per_unit!=10000`：进入单独的“调整合约”数据质量区，不并入标准合约墙位。
- 页面同时给出调整合约数量、总 OI、行权价和实际 `per_unit`。
- 若某期限只有调整合约，没有标准合约，该期限显示“无标准合约墙位”，而不是返回 0。

这样能够保持第一阶段 OI Wall 的定义直观，后续计算 Gamma/GEX 时再按每张合约的实际 `per_unit` 计算经济暴露。

## 7. 计算口径

设到期日为 `T`，行权价为 `K`，期权方向为 `S∈{Call, Put}`。

### 7.1 行权价 OI

```text
StrikeOI(T, K, S) = Σ OI_i
```

求和范围是相同到期日、相同行权价、相同 Call/Put 方向的标准合约。

- `OI=0` 是有效观测，保留为 0。
- `OI IS NULL` 是数据缺失，不转换成 0；该合约从聚合值中排除并计入质量告警。
- 原始 OI 的单位是“张”。

### 7.2 OI Wall

```text
WallOI(T, S) = max_K StrikeOI(T, K, S)
WallStrike(T, S) = {K | StrikeOI(T, K, S) = WallOI(T, S)}
```

如果多个行权价并列最大，全部列为墙位，不用“离现货最近”等二次条件静默挑选一个。

### 7.3 墙位距现货

```text
DistancePct(K) = (K / Spot - 1) × 100%
```

- `Spot` 使用 `dataDate` 当日的 `close_qfq`。
- 正值表示行权价高于现货，负值表示低于现货。
- 该值只是空间位置，不代表价格将向墙位移动或在墙位反转。

### 7.4 Top 3 集中度

```text
Top3Share(T, S) = Top 3 StrikeOI 之和 / 该期限该方向全部 StrikeOI 之和
```

分母为 0 时返回 `null`，页面显示“不可计算”，不返回 0%。

### 7.5 标的份额辅助值

调整合约区可以展示：

```text
UnderlyingUnits_i = OI_i × per_unit_i
```

该值只用于解释合约乘数差异，不替代第一阶段标准合约 OI Wall 的“张数”口径。

## 8. 纯计算输入输出

核心计算函数接收已经规范化的字典行，不在函数内部访问 Flask 或数据库：

```python
buildOiWallSnapshot(
    optionRows: list[dict[str, object]],
    *,
    dataDate: date,
    spot: float,
) -> dict[str, object]
```

主要输出结构：

```json
{
  "underlying": "510050.SH",
  "dataDate": "2026-09-22",
  "spot": 3.015,
  "spotField": "close_qfq",
  "expiries": [
    {
      "maturityDate": "2026-10-28",
      "dteCalendar": 36,
      "call": {
        "wallStrikes": [3.10],
        "wallOi": 72257,
        "top3Share": 0.0,
        "strikes": []
      },
      "put": {}
    }
  ],
  "quality": {
    "source": "stock_tushare.opt_basic + opt_daily + etf_daily",
    "optionRows": 0,
    "standardRows": 0,
    "adjustedRows": 0,
    "missingOiRows": 0,
    "dateAligned": true
  }
}
```

`top3Share` 示例中的 `0.0` 仅表示类型；真实结果由数据计算，不能硬编码。

## 9. HTTP 接口

### `GET /`

返回独立教学页面。

### `GET /api/oi-wall`

返回最新共同数据日的完整快照。

- 成功：HTTP 200，`ok=true`。
- 无共同数据日：HTTP 503，`ok=false`，给出可读错误。
- 数据库不可达：HTTP 503，不泄露连接字符串或凭据。
- 计算数据不完整：有结果时仍返回 200，并在 `quality.warnings` 中列明；无法形成任何期限时返回 422。

### `GET /health`

只报告进程和数据库只读探测状态，不执行完整 OI 计算，不返回凭据。

## 10. 页面结构

页面按“先看结果，再看计算，再看限制”组织：

1. **页头**
   - 510050 上证 50 ETF 期权墙。
   - 数据日期、现货 `close_qfq`、数据源、EOD 标签。
2. **四期限导航**
   - 到期日、剩余自然日、合约数量、总 OI。
3. **OI Wall 主图**
   - Call 在左、Put 在右，行权价共用中轴。
   - 用水平长度表达 OI，现货相邻行权价有清晰标记。
   - Wall 和 Top 3 使用不同层级，不用大面积高饱和底色。
4. **计算过程**
   - 选择一个方向和行权价后显示原始合约、`ΣOI`、与其他行权价的排序、墙位判定。
5. **如何理解**
   - 说明 OI 是存量，不包含持仓方向和新开/平仓来源。
   - 说明墙位是集中度，不自动等同阻力或支撑。
6. **数据质量**
   - 标准/调整合约数量、缺失 OI、共同日期校验、被排除行数。

页面采用紧凑研究终端风格，红色和绿色不表达主观“好坏”；Call、Put 使用固定、可访问的中性色系，避免与 A 股涨跌颜色混淆。

## 11. 错误与边界处理

- 数据库连接必须使用项目既有环境变量配置，不在新文件写入密码。
- SQL 参数化；510050 标的常量固定，但仍不拼接用户输入。
- 数据库连接在 `finally` 中关闭。
- `exercise_price`、`oi`、`per_unit` 必须做有限数值校验。
- `spot<=0` 时拒绝计算距离百分比。
- 到期日早于 `dataDate` 的合约不进入当前四期限。
- 同一 `ts_code + trade_date` 重复时视为质量错误，不静默重复求和。
- 页面空态、接口错误和日志使用相同错误编号，便于排查。

## 12. 测试设计

测试先于实现，至少覆盖：

1. Call、Put 按期限和行权价正确聚合。
2. Wall 最大值和并列墙位。
3. Top 3 集中度及分母为 0。
4. 距离现货的正负方向。
5. `OI=0` 保留、`OI=None` 排除并告警。
6. `per_unit!=10000` 不进入主墙位，调整合约仍出现在质量明细。
7. 只选择期权和 ETF 均存在的最新共同交易日。
8. 只返回最近四个有数据的未到期期限。
9. 数据库异常不会暴露密码。
10. Flask 页面、API、健康检查路由可访问。
11. 对本地数据库做只读集成探测，核对 `dataDate`、四期限和记录数。
12. 用浏览器检查主图、展开计算过程、窄屏和空态。

## 13. 实施顺序

1. 先写纯函数测试，确认测试因缺少实现而失败。
2. 实现数据规范化和 OI Wall 计算，跑通纯函数测试。
3. 实现只读查询和共同数据日选择，跑数据库定点探测。
4. 实现 JSON API 与健康检查。
5. 实现页面和计算过程交互。
6. 启动独立服务，浏览器验证真实 510050 数据。
7. 运行项目要求的交付自检、全量测试和 scratch retention 审计。

## 14. 验收证据

第一阶段完成时必须提供：

- 测试先失败、实现后通过的具名测试输出。
- `/api/oi-wall` 的精简真实返回，包括 `dataDate`、spot、四期限墙位和质量字段。
- 至少一个行权价从原始合约到 `ΣOI` 再到墙位排序的可复现证据。
- 页面截图或浏览器实际元素检查。
- `python -X utf8 scripts/selfcheck_delivery.py` 结果。
- `python -m pytest tests/ -q` 结果。
- `python -X utf8 scripts/audit_scratch_retention.py --sweep` 结果。
- 明确列出未能验证的部分。

## 15. 后续扩展顺序

保持同一独立页面和数据契约，逐项扩展：

```text
OI Wall
→ 1D/3D/5D Flow Wall
→ Max Pain
→ Absolute Gamma Wall
→ Gamma Migration
→ Estimated Dealer GEX / Gamma Flip
→ 历史回测
```

每一项都必须新增独立的公式说明、数据质量字段和回归测试；Estimated Dealer 指标必须与可观测 OI/Gamma 指标在命名和视觉上明确分层。
