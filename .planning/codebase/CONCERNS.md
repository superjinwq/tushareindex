# Concerns

**Analysis Date:** 2026-04-19

## Scale & maintenance

- **超大单文件**：`rt_monitor_app.py`、`fina_indicator.py` 行数极高，合并冲突与审查成本高。
- **qlib 子树**：体积极大，克隆/索引负担高；日常功能可忽略其内部。

## External dependencies

- **Tushare**：积分/频次限制；`top10_*` 需 2000+ 积分；全市场按票循环耗时极长。
- **Windows**：控制台编码、日志轮转 `PermissionError`（已用安全 Handler 缓解）。

## Data correctness

- 财务/股东类存在 **修订与多次公告**；唯一键与 PIT 查询需与现表一致。
- **top10**：`ts_code` 必选 → 无法单请求全市场，需注意全量耗时与补数策略。

## Security

- Token/DB 口令应走环境变量；勿将生产密钥写入可公开脚本（历史示例密码需替换）。
