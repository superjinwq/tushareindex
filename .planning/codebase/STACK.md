# Technology Stack

**Analysis Date:** 2026-04-19

## Languages

**Primary:**
- Python 3.12 - 全部业务逻辑、数据同步、Web 服务

**Secondary:**
- SQL (MySQL 方言) - 数据查询与 DDL，散落于各 `.py` 文件和 `sql/` 目录

**Frontend:**
- HTML/CSS/JavaScript - `templates/` 和 `static/` 下的 Web 监控页

## Runtime

**Environment:**
- Python 3.12（`pyproject.toml` 中 `requires-python = ">=3.12"`）
- Windows 11（开发与生产运行环境相同）

**Package Manager:**
- `uv`（`uv.lock` 存在，`pyproject.toml` 使用 `[dependency-groups]`）
- 备用：pip（`requirements.txt` / `requirements_python_mysql.txt`）
- Lockfile: `uv.lock`（已提交）

## Frameworks

**Web 服务:**
- Flask 3.1.3 - 行情监控 Web 服务（`rt_monitor_app.py`、`web_dashboard.py`）
- flask-socketio 5.3.x / 已安装 2.9.0.dev - 实时推送（`SocketIO(app, cors_allowed_origins="*")`，默认端口 5000，由 `RT_MONITOR_PORT` 控制）
- Werkzeug 3.0.x - WSGI 工具层（Flask 依赖）

**数据处理:**
- pandas 2.4.2 - 全量数据分析与批量写入，核心库
- numpy 2.4.2 - 向量化计算，与 pandas 配套使用
- SQLAlchemy 2.0.47 - ORM / `create_engine` 方式批量写入 DataFrame（`to_sql`），部分脚本直接用 `pymysql`

**Testing（dev 依赖）:**
- pytest 9.0.2 - `pyproject.toml` dev 组
- ruff 0.14.14 - lint（dev 组）
- ty 0.0.14 - 类型检查（dev 组）

**Build / Dev:**
- 无 CI 管道，手动 `python script.py` 运行；批处理 `.bat` 和 PowerShell `.ps1` 做快捷启动

## Key Dependencies

**Critical（运行时必须）:**
- `tushare` 1.4.24 - Tushare Pro 行情接口，`ts.pro_api(token)`，几乎所有同步脚本依赖
- `pymysql` 1.4.6 - MySQL 连接驱动，主要连接方式
- `mysql-connector-python`（`mysql.connector`，部分脚本用）- 备用 MySQL 驱动
- `pandas` 2.4.2 - 数据处理核心
- `Flask` 3.1.3 + `flask-socketio` - Web 监控核心

**数据读写辅助:**
- `requests` 2.32.5 - HTTP 请求（巨潮、GLM API）
- `pdfplumber` 0.11.x - PDF 表格抽取（`cninfo_ann_glm_digest.py`，可选）
- `openpyxl` - Excel 读写（含 Wind 数据导入）
- `xlrd` - 旧 `.xls` 格式读取（部分历史脚本）
- `reportlab` 4.4.4 - PDF 报告生成（ETF 基金分析报告、融资融券报告）
- `markdown` 3.4.x - Markdown 渲染（Web 模板）

**量化/ML（可选）:**
- `scipy` - 统计与稀疏矩阵（特征工程脚本）
- `scikit-learn` 1.7.1 - 聚类（KMeans）、特征选择、StandardScaler、PCA（`08_*.py` 系列）
- `torch` 2.9.0.dev（CUDA 12.8）- 深度学习（`12_深度学习*.py`、`best_pytorch_model.pth`）
- `cupy` - GPU 加速计算（`08_数据库实时股票筛选_CuPy优化版.py`，可选）
- `tensorflow`/`keras` - 旧模型（`best_model.h5`，脚本已有 `fix_tensorflow_conflict.py`，非主流路径）

**通达信数据读取:**
- `pytdx` 0.11.7 - 通达信本地文件读取（`V300C0_日线导出_pytdx_mootdx.py`）
- `mootdx` - 通达信本地 Reader（同上）

**Wind 数据（需 Wind 终端）:**
- `WindPy`（`from WindPy import w`）- Wind 金融终端 Python 接口（`wind1min.py`、`indexwind全A.py` 等，需 Wind 客户端安装）

## Configuration

**Environment:**
- 主要通过**环境变量**传递密钥和连接参数
- `TUSHARE_TOKEN` - Tushare API Token（必填）
- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` - MySQL 连接（可选，脚本中有默认值 localhost/root/123456）
- `RT_MONITOR_PORT` - Web 服务端口（默认 5000）
- `RT_MONITOR_AUTO_KILL_PREVIOUS` - 是否自动清理旧进程（默认 0）
- `RT_MONITOR_API_PREFIX` - 反代前缀路径
- `ZHIPU_API_KEY` - 智谱 GLM API Key（`cninfo_ann_glm_digest.py` 使用）
- `jinwq.env` 文件保存本地开发默认值（**勿提交真实密码**）
- `config.json` - 部分脚本使用的 JSON 配置（API 重试、连接池参数）

**Build:**
- `pyproject.toml` - uv/ruff/ty 配置
- `uv.lock` - 锁定依赖版本

## Platform Requirements

**Development:**
- Windows 11，Python 3.12
- MySQL 8.4.5（本地），`innodb_buffer_pool_size=4096M`
- Wind 终端（仅 Wind 系列脚本需要）
- NVIDIA GPU + CUDA 12.x（仅 ML 脚本需要，主业务不依赖）

**Production:**
- 同开发环境（本地运行，无云端部署）
- 通过 `.bat` / `.ps1` 定时触发或手动执行
- Web 服务 `rt_monitor_app.py` 以 `socketio.run(host='0.0.0.0', port=5000)` 运行，可局域网访问

---

*Stack analysis: 2026-04-19*
