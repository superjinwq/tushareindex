# vol_price_screener 筛选条件核对说明（依据对话日志）

## 一、对话日志中确认的完整筛选条件

### 1. 数据来源与预处理
- **量价数据**：`stk_factor_pro`（ts_code, trade_date, pct_chg, volume_ratio, close_qfq, high_qfq, low_qfq, amount, 及可选 rsi_qfq_6/12, boll_* 等）
- **均线数据**：`stk_factor_ma`（ema20, ema60, ema120, ma20, ma60, ma120）
- **第一道过滤**：MA+EMA 双重多头排列（均线多头）后的股票池

### 2. 手工量价规则（calcStockScore）

| 项目 | 条件 | 说明 |
|------|------|------|
| **综合得分公式** | score = 涨日放量比例×40 + 跌日缩量比例×40 + 量价相关系数×20 | 满分 100，用于排序 |
| 涨日放量 | ≥ 50% | 曾为 60%，后按统计改为 50% |
| 跌日缩量 | ≥ 60% | |
| 量价相关 | ≥ 0.3 | |
| 窗口涨幅 | < 30% | 曾为 50%，后按统计改为 30% |
| BOLL 位置 | ≥ 40%，无上限 | 上限曾为 95%，后改为 99.9 等效无上限 |
| RSI12 | ≤ 75，无下限 | 下限曾为 40，后取消（0） |
| 近 5 日日均成交额 | ≥ 3 亿 | amount 单位千元，3 亿 = 300000 千元 |

### 3. 行业与融资融券
- **mergeIndustry**：按 ts_code 映射 `industry_sw_2021_2`（来自 stockcodes，一般用 conn_wind）
- **mergeMarginScore**：调用 `fetchIndustryMarginScores(conn_tushare, conn_wind, trade_date)` 得到行业→[0,1] 得分，按标的的 industry_sw_2021_2 映射到每只股票
- **主流程**：main 中先 mergeIndustry 再 mergeMarginScore，Top20 的融资得分即来自此

### 4. 输出与配置
- **窗口**：WINDOW_LIST，如 [20] 或 [30]
- **输出**：全量 CSV（vol_price_20d_rule_YYYYMMDD.csv）、Top N（如 Top20）CSV
- **列**：股票代码、涨日放量、跌日缩量、相关系数、综合评分、窗口涨跌幅、最大振荡幅、回落幅（可选 ML 概率列）

### 5. 可选 ML 模式
- USE_ML_MODEL 开关、loadMlModel、calcMlScore（与 vol_price_ml_optimizer 一致的特征与模型推理）

---

## 二、当前 vol_price_screener.py 已恢复 / 未恢复项

| 项目 | 对话日志中是否存在 | 当前脚本是否恢复 |
|------|-------------------|------------------|
| 100 分权重表（注释） | 是（你提供的表） | ✅ 已恢复 |
| fetchIndustryMarginScores | 是 | ✅ 已恢复 |
| fetchVolPriceData（stk_factor_pro + stk_factor_ma） | 是 | ❌ 未恢复 |
| MA+EMA 双重多头过滤 | 是 | ❌ 未恢复 |
| calcStockScore（量价 40+40+20 + 各硬性过滤） | 是 | ❌ 未恢复 |
| 涨日放量≥50%、跌日缩量≥60%、量价相关≥0.3 | 是 | ❌ 未恢复 |
| 窗口涨幅<30%、BOLL≥40%、RSI12≤75 | 是 | ❌ 未恢复 |
| 近 5 日日均成交额≥3 亿（amount 千元） | 是 | ❌ 未恢复 |
| mergeIndustry / mergeMarginScore | 是 | ❌ 未恢复 |
| runScreener、main、CSV 输出 | 是 | ❌ 未恢复 |
| ML 模式（USE_ML_MODEL、calcMlScore） | 是 | ❌ 未恢复 |

**结论**：当前脚本**仅恢复了「融资融券」的 fetchIndustryMarginScores 及 100 分权重表注释**，其余筛选条件与主流程均未恢复。需要按上述列表补全方可与对话日志中的设计一致。

---

（依据对话 [30e51d3b] 整理）
