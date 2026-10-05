# RT_MONITOR_STATIC_VER 历史

自 2026-10-04 起版本号改为按 `static/` + `templates/` 内容哈希自动生成（见 `rt_monitor/core/config.py` 的 `computeRtMonitorStaticVer`），
不再手工递增。以下为此前手工递增时代写在 `config.py` 里的历史注释，逐行原样迁移，未作删改。

## 原 `config.py` 版本号定义上方注释（134 行）

```text
# 代码更新时间：2026-09-11 08:40 RT_MONITOR_STATIC_VER 20260911-1
#   （T 板字段级 as-of 闸门上线：跨日/无时间行情不再冒充实时）
# 代码更新时间：2026-09-11 11:20 RT_MONITOR_STATIC_VER 20260911-2
#   （T 板 A2 字段级灰阶陈旧度：最新/成交量/持仓量 >10s 淡灰、>60s 深灰）
# 代码更新时间：2026-09-11 14:0x RT_MONITOR_STATIC_VER 20260911-3
#   （A2 阈值改 30s/300s；非可见链 6s 后台低频刷新，可见链仍 800ms）
# 代码更新时间：2026-09-12 00:1x RT_MONITOR_STATIC_VER 20260912-1
#   （Sell Put 计划区现货 chip 明确口径「标的收盘 + 行情日」，与预演区实时现货区分，避免多口径歧义）
# 代码更新时间：2026-09-14 RT_MONITOR_STATIC_VER 20260914-1
#   （T 板行情字段走 1 秒轻量 delta；整表 IV/希腊值保留 5 秒重算，浏览器可识别新构建）
# 代码更新时间：2026-09-15 18:26 RT_MONITOR_STATIC_VER 20260915-1
#   （T 板 IVR Delta 展示范围调整为 |Delta| 0.10-0.90）
# 代码更新时间：2026-09-15 18:55 RT_MONITOR_STATIC_VER 20260915-2
#   （T 板段头分开本地观测与最后市场事件，避免盘后把本地刷新误作行情实时）
# 代码更新时间：2026-09-20 20:5x RT_MONITOR_STATIC_VER 20260920-1
#   （T 板 IVR → Per 1y/C/A 三列；IV 看板 Per → Per A/C/1y；两处统一走 opt_iv_windows）
# 代码更新时间：2026-09-20 21:4x RT_MONITOR_STATIC_VER 20260920-2
#   （义务仓优选分位列去掉 ☆：远月分位系统性偏高，四期限最高恒为下季无区分度）
# 代码更新时间：2026-09-20 22:1x RT_MONITOR_STATIC_VER 20260920-3
#   （优选 Per mid 加浅蓝底；T 表行权价去 ☆；量仓 ≥1 万显示 x.x万）
# 代码更新时间：2026-09-21 06:2x RT_MONITOR_STATIC_VER 20260921-1
#   （平值橙底不得盖住量仓 top3 着色）
# 代码更新时间：2026-09-21 06:31 RT_MONITOR_STATIC_VER 20260921-2
#   （T 板顶栏 Per C 锚点入口：页面 JSON 覆盖，保存即生效）
# 代码更新时间：2026-09-21 06:54 RT_MONITOR_STATIC_VER 20260921-3
#   （T/义务仓加宽：并排断点 2200→3360；IV 分位列收紧 1626→1542）
# 代码更新时间：2026-09-21 07:03 RT_MONITOR_STATIC_VER 20260921-4
#   （IV ≥100% 一位小数 123.0；+3σ 列 52→46）
# 代码更新时间：2026-09-21 07:12 RT_MONITOR_STATIC_VER 20260921-5
#   （T 型 IV≥100 取整让宽给时值；义务仓 ┊ 居中对齐）
# 代码更新时间：2026-09-21 07:22 RT_MONITOR_STATIC_VER 20260921-6
#   （撤销 3360 断行：桌面 nowrap，右栏最多 46%，T/IV 左右并排）
# 代码更新时间：2026-09-21 07:34 RT_MONITOR_STATIC_VER 20260921-7
#   （义务仓表头单行、去掉 1760 横滚、收紧留白）
# 代码更新时间：2026-09-21 07:41 RT_MONITOR_STATIC_VER 20260921-8
#   （表头整段居中不换行，┊ 只对齐格子不齐表头）
# 代码更新时间：2026-09-21 07:45 RT_MONITOR_STATIC_VER 20260921-9
#   （档位列加宽留白；义务仓字号与 T 型一致）
# 代码更新时间：2026-09-21 07:52 RT_MONITOR_STATIC_VER 20260921-10
#   （右栏按 IV 完整宽度分配，表 100% 铺满不再横滚）
# 代码更新时间：2026-09-21 08:07 RT_MONITOR_STATIC_VER 20260921-11
#   （义务仓 Per 1y/C/A 用期限列留白拉开间距）
# 代码更新时间：2026-09-21 08:15 RT_MONITOR_STATIC_VER 20260921-12
#   （Per 与☆、有效价与┊ 不重叠；档位列右留白收紧）
# 代码更新时间：2026-09-21 08:25 RT_MONITOR_STATIC_VER 20260921-13
#   （义务仓 ┊ 同列对齐；四期限浅底色分层）
# 代码更新时间：2026-09-21 08:35 RT_MONITOR_STATIC_VER 20260921-14
#   （义务仓左侧轨宽贴数字，余量给 Per 1y/C/A + ☆）
# 代码更新时间：2026-09-21 08:47 RT_MONITOR_STATIC_VER 20260921-15
#   （义务仓平值行琥珀金加描边，期限浅底仍可辨）
# 代码更新时间：2026-09-22 13:18 RT_MONITOR_STATIC_VER 20260922-1
#   （T型顶栏 chips 同步 boot，不依赖 DOMContentLoaded/defer）
# 代码更新时间：2026-09-22 13:28 RT_MONITOR_STATIC_VER 20260922-2
#   （权利仓页签文案；波动型档位实三–虚四）
# 代码更新时间：2026-09-22 13:47 RT_MONITOR_STATIC_VER 20260922-3
#   （波动型 Per 改该档；翼部过门虚线提示）
# 代码更新时间：2026-09-22 15:11 RT_MONITOR_STATIC_VER 20260922-4
#   （权利仓规则浓缩进标题行）
# 代码更新时间：2026-09-22 17:51 RT_MONITOR_STATIC_VER 20260922-5
#   （权利仓页签按标的 localStorage 记忆）
# 代码更新时间：2026-09-22 18:07 RT_MONITOR_STATIC_VER 20260922-6
#   （剩余天数附交易日 36天.X）
# 代码更新时间：2026-09-22 18:15 RT_MONITOR_STATIC_VER 20260922-7
#   （trade_cal 未覆盖到期不展示交易日）
# 代码更新时间：2026-09-23 05:09 RT_MONITOR_STATIC_VER 20260923-1
#   （T板 Δ 三位小数；义务仓轨宽/Δ列宽微调）
# 代码更新时间：2026-09-23 05:20 RT_MONITOR_STATIC_VER 20260923-2
#   （义务仓/权利仓 metrics 左右疏密重排）
# 代码更新时间：2026-09-23 05:24 RT_MONITOR_STATIC_VER 20260923-3
#   （选择器改 fr 比例分配，消除固定 em 空档/溢出）
# 代码更新时间：2026-09-23 05:28 RT_MONITOR_STATIC_VER 20260923-4
#   （义务/权利仓 metrics 上下分行，协调疏密）
# 代码更新时间：2026-09-23 05:33 RT_MONITOR_STATIC_VER 20260923-5
#   （撤回换行：单行 metrics；左栏加宽、右栏 IV 上限 1080）
# 代码更新时间：2026-09-23 05:36 RT_MONITOR_STATIC_VER 20260923-6
#   （单行比例分配；右栏 IV 上限 960，左栏再加宽）
# 代码更新时间：2026-09-23 05:41 RT_MONITOR_STATIC_VER 20260923-7
#   （单行贴内容；metrics 0.74rem；右栏 640、行权列 10.4rem）
# 代码更新时间：2026-09-23 05:42 RT_MONITOR_STATIC_VER 20260923-8
#   （单行 flex：左贴指标 ┊ 贴左，Per 贴右不拉开）
# 代码更新时间：2026-09-23 05:45 RT_MONITOR_STATIC_VER 20260923-9
#   （右栏 520、行权 9.8rem、字轨再收，1600 宽单行不溢）
# 代码更新时间：2026-09-23 05:47 RT_MONITOR_STATIC_VER 20260923-10
#   （恢复右栏 IV 1560 / 主栏 40%；metrics 仅单行格内，不动页面左右宽）
# 代码更新时间：2026-09-23 05:53 RT_MONITOR_STATIC_VER 20260923-11
#   （期限格宽不变：左三轨各 +3px，Per 三列自动各约 −3px）
# 代码更新时间：2026-09-23 05:58 RT_MONITOR_STATIC_VER 20260923-12
#   （左三轨再 +3px 累计 +6px 进内容轨；Per 再各约 −3px）
# 代码更新时间：2026-09-23 06:11 RT_MONITOR_STATIC_VER 20260923-13
#   （快照说明跟 Per C；chip 下最新价三位+红涨绿跌）
# 代码更新时间：2026-09-23 06:46 RT_MONITOR_STATIC_VER 20260923-14
#   （T 表 Γ 两位，与期权 Δ 同重算；Call 在 Δ 左、Put 镜像）
# 代码更新时间：2026-09-23 06:54 RT_MONITOR_STATIC_VER 20260923-15
#   （IV 看板中轴 IV−HV 表头由 Δ 改为 溢）
# 代码更新时间：2026-09-23 07:09 RT_MONITOR_STATIC_VER 20260923-16
#   （T 板 chip 增 Intra，与主页 Ma/Ema 方向表同式）
# 代码更新时间：2026-09-23 07:21 RT_MONITOR_STATIC_VER 20260923-17
#   （T 板顶栏距浏览器/框内上下 1px，标题垂直居中）
# 代码更新时间：2026-09-23 07:36 RT_MONITOR_STATIC_VER 20260923-18
#   （义务仓优选与权利仓观察竖缝 1px）
# 代码更新时间：2026-09-23 07:53 RT_MONITOR_STATIC_VER 20260923-19
#   （Sell Put 首屏早发请求 + 自启动，不再等 chart-stack）
# 代码更新时间：2026-09-23 07:59 RT_MONITOR_STATIC_VER 20260923-20
#   （壳页已嵌入计划则跳过 head 抢跑，避免和静态脚本抢 Flask）
# 代码更新时间：2026-09-23 09:31 RT_MONITOR_STATIC_VER 20260923-21
#   （chip 第二行 Gap + 全样本 expanding Per，与计划区 gapP 同源）
# 代码更新时间：2026-09-23 10:16 RT_MONITOR_STATIC_VER 20260923-22
#   （顶栏现货壳页内嵌 + T 表 API 带 spots，不再只靠 quote-delta）
# 代码更新时间：2026-09-23 10:32 RT_MONITOR_STATIC_VER 20260923-23
#   （后台整表单飞，quote-delta 高优先级，同标的静默不再作废盘口）
# 代码更新时间：2026-09-23 10:48 RT_MONITOR_STATIC_VER 20260923-24
#   （非可见链预取串行，避免四路整表同时占满连接池）
# 代码更新时间：2026-09-23 11:40 RT_MONITOR_STATIC_VER 20260923-25
#   （T 板隐藏页停轮询+超时自恢复；Ma/Ema 实时行情与慢技术缓存解耦）
# 代码更新时间：2026-09-23 13:33 RT_MONITOR_STATIC_VER 20260923-26
#   （权利仓波动型只列出至少一个期限入选的档）
# 代码更新时间：2026-09-23 19:0x RT_MONITOR_STATIC_VER 20260923-27
#   （T 板后台轮询不再主动 abort；Ma/Ema 完整信号重算隔离到单 worker 进程）
# 代码更新时间：2026-09-24 11:39 RT_MONITOR_STATIC_VER 20260924-1
#   （义务仓优选只列出至少一个期限入选的档）
# 代码更新时间：2026-10-01 12:45 RT_MONITOR_STATIC_VER 20261001-1
#   （成交额表 T-120 后增全历史 MAX / 峰值日）
# 代码更新时间：2026-10-01 13:34 RT_MONITOR_STATIC_VER 20261001-2
#   （成交额表：缩量%两端、峰值日重合、谷值日=T-1、T≥T-1 着色）
# 代码更新时间：2026-10-01 13:44 RT_MONITOR_STATIC_VER 20261001-3
#   （成交额表 T 列：低于昨日全天改为绿色）
# 代码更新时间：2026-10-01 14:07 RT_MONITOR_STATIC_VER 20261001-4
#   （涨跌幅表 Gap 旁注全历史分位；Pct/Intra/Gap/Amp 表头公式）
# 代码更新时间：2026-10-01 15:21 RT_MONITOR_STATIC_VER 20261001-5
#   （涨跌幅表 Pct/Intra/Gap/Amp 全历史分位；≤5 绿底 ≥95 红底）
# 代码更新时间：2026-10-01 16:45 RT_MONITOR_STATIC_VER 20261001-6
#   （Ma/Ema 表 AMT/Pct/Intra/Gap/Amp 全历史分位与尾部浅底）
# 代码更新时间：2026-10-01 16:50 RT_MONITOR_STATIC_VER 20261001-7
#   （分位旁注固定两位数字宽，与原字段隔开）
```

## 原 `config.py` 文件末尾注释（186 行）

```text
# 代码更新时间：2026-06-22 22:24
# 代码更新时间：2026-07-09 09:41 RT_MONITOR_STATIC_VER 20260709-1（财报表虚拟滚动+进度条+API gzip）
# 代码更新时间：2026-07-20 17:55 RT_MONITOR_STATIC_VER 20260720-1（市场状态跌停开右增 HisHC/HisLC）
# 代码更新时间：2026-07-20 18:06 RT_MONITOR_STATIC_VER 20260720-2（HisHC 弹窗：左 HisHC/HisLC + 右 399317）
# 代码更新时间：2026-07-20 18:34 RT_MONITOR_STATIC_VER 20260720-3（120/200/250/850/HisHLC 各自弹窗+右轴 399317）
# 代码更新时间：2026-07-20 19:13 RT_MONITOR_STATIC_VER 20260720-4（250DHLC 弹窗左轴改为 HL250_CCCC 五日均值）
# 代码更新时间：2026-07-20 19:19 RT_MONITOR_STATIC_VER 20260720-5（250DHLC 弹窗左轴改为三日均值）
# 代码更新时间：2026-07-20 19:27 RT_MONITOR_STATIC_VER 20260720-6（250DHLC 弹窗左轴改回五日均值）
# 代码更新时间：2026-07-20 19:35 RT_MONITOR_STATIC_VER 20260720-7（120/200/850/HisHLC 弹窗左轴亦五日均值）
# 代码更新时间：2026-07-20 19:47 RT_MONITOR_STATIC_VER 20260720-8（HisHC 弹窗 HisHC/HisLC 亦五日均值）
# 代码更新时间：2026-07-20 22:17 RT_MONITOR_STATIC_VER 20260720-9（申万页 250/850/His低 明细弹窗增申万行业）
# 代码更新时间：2026-07-25 21:00 RT_MONITOR_STATIC_VER 20260725-1（fina 首次his/850 增距前高 gap_to_peak_pct；scroll 266列）
# 代码更新时间：2026-07-25 21:17 RT_MONITOR_STATIC_VER 20260725-2（修复距前高筛选 matcher 未解包导致 fina 0 条）
# 代码更新时间：2026-07-25 21:54 RT_MONITOR_STATIC_VER 20260725-3（20日换手/距前高信号着色）
# 代码更新时间：2026-07-25 22:11 RT_MONITOR_STATIC_VER 20260725-4（首次his/850组头因子提示弹窗可拖可缩）
# 代码更新时间：2026-07-27 11:54 RT_MONITOR_STATIC_VER 20260727-1（申万收盘价六列弹窗统一申万行业+突破类型）
# 代码更新时间：2026-07-27 12:30 RT_MONITOR_STATIC_VER 20260727-2（牛散方案汇总条每行固定 7 个换行）
# 代码更新时间：2026-07-29 15:47 RT_MONITOR_STATIC_VER 20260729-1（主营构成同比改上年同期 yoy_base）
# 代码更新时间：2026-08-04 15:03 RT_MONITOR_STATIC_VER 20260804-1（财报页新增「当前价位置」7 列，scroll 266→273）
# 代码更新时间：2026-08-04 15:18 RT_MONITOR_STATIC_VER 20260804-2（历史最低改倍数列 mult_vs_h_low）
# 代码更新时间：2026-08-05 06:35 RT_MONITOR_STATIC_VER 20260805-1（财报页自公告% pct_since_ann，scroll 274）
# 代码更新时间：2026-08-05 07:04 RT_MONITOR_STATIC_VER 20260805-2（修复 mPctSinceAnn 未解包导致 fina 0 条）
# 代码更新时间：2026-08-05 07:13 RT_MONITOR_STATIC_VER 20260805-3（自公告% 正红负绿同 T5/T10）
# 代码更新时间：2026-08-05 09:01 RT_MONITOR_STATIC_VER 20260805-4（点击股票代码标记感兴趣，localStorage）
# 代码更新时间：2026-08-05 09:11 RT_MONITOR_STATIC_VER 20260805-5（感兴趣标记落盘 fina_interest_marks.json）
# 代码更新时间：2026-08-05 22:20 RT_MONITOR_STATIC_VER 20260805-6（opt-t-board 分区标题左侧显示行情日）
# 代码更新时间：2026-08-05 22:46 RT_MONITOR_STATIC_VER 20260805-7（opt-t-board 增加 510050 上证50 展示档案）
# 代码更新时间：2026-08-05 23:11 RT_MONITOR_STATIC_VER 20260805-8（opt-t-board 右侧固定 IV 看板）
# 代码更新时间：2026-08-05 23:34 RT_MONITOR_STATIC_VER 20260805-9（铁律移 IV 下、顶栏 sticky、IV 字号、−3σ≤0）
# 代码更新时间：2026-08-05 23:43 RT_MONITOR_STATIC_VER 20260805-10（σ改经验分位、顶栏单行、收紧列宽页边）
# 代码更新时间：2026-08-05 23:51 RT_MONITOR_STATIC_VER 20260805-11（左收内容宽、右加宽、IV/T 字号行距统一）
# 代码更新时间：2026-08-06 00:00 RT_MONITOR_STATIC_VER 20260805-12（左右按内容宽等比收缩、单元格横向留白统一 6px）
# 代码更新时间：2026-08-06 00:08 RT_MONITOR_STATIC_VER 20260806-1（右栏按 IV 内容定宽、左栏吃剩余、T 型逐列 fixed 四区块对齐）
# 代码更新时间：2026-08-06 00:14 RT_MONITOR_STATIC_VER 20260806-2（IV 五块逐列 fixed 对齐 1156px、T 型 720px，1920 下无横滚）
# 代码更新时间：2026-08-06 01:05 RT_MONITOR_STATIC_VER 20260806-3（T 型列宽按五标的实测重排 716px、价差去千分位、<1902px 改堆叠）
# 代码更新时间：2026-08-06 07:07 RT_MONITOR_STATIC_VER 20260806-4（弹性排序与警示合并一行、IV 购/沽改最新 IV 值、去区块日期行、IV 表头单行、日期紧凑 6 位）
# 代码更新时间：2026-08-06 07:28 RT_MONITOR_STATIC_VER 20260806-5（IV 五块列宽重排对齐、Max≥100 一位小数、禁横滚）
# 代码更新时间：2026-08-06 07:33 RT_MONITOR_STATIC_VER 20260806-6（T 型成交量/前收盘/最新价各加 1 位数字宽，表宽 764）
# 代码更新时间：2026-08-06 07:40 RT_MONITOR_STATIC_VER 20260806-7（IV 表宽 1256、中轴 Per/最新/标的优先加宽并略放大字号）
# 代码更新时间：2026-08-06 08:05 RT_MONITOR_STATIC_VER 20260806-8（IV 最新值弹窗：全样本 IV%+ETF 双轴、可拖可缩）
# 代码更新时间：2026-08-06 08:36 RT_MONITOR_STATIC_VER 20260806-9（最新越过 ±2σ/±3σ 时 σ 列醒目着色）
# 代码更新时间：2026-08-06 09:12 RT_MONITOR_STATIC_VER 20260806-10（申万页表头融资余额同行显示数据日 YYYYMMDD）
# 代码更新时间：2026-08-06 10:15 RT_MONITOR_STATIC_VER 20260806-11（申万区间涨跌幅 T 点击弹窗：涨幅/成交额 Top10）
# 代码更新时间：2026-08-06 10:51 RT_MONITOR_STATIC_VER 20260806-12（成交额 Top10 增涨幅%）
# 代码更新时间：2026-08-06 11:01 RT_MONITOR_STATIC_VER 20260806-13（涨幅 Top10 增成交额）
# 代码更新时间：2026-08-06 11:06 RT_MONITOR_STATIC_VER 20260806-14（Top20 + 距历史/850/250高）
# 代码更新时间：2026-08-06 14:30 RT_MONITOR_STATIC_VER 20260806-15（Top 弹窗成交额/市值去亿、自由换手去%）
# 代码更新时间：2026-08-06 18:12 RT_MONITOR_STATIC_VER 20260806-16（申万页：新高低缓存TTL拉长+轮询防叠）
# 代码更新时间：2026-08-06 18:26 RT_MONITOR_STATIC_VER 20260806-17（申万页 SWR 秒开+5s 轮询）
# 代码更新时间：2026-08-06 18:50 RT_MONITOR_STATIC_VER 20260806-18（修复新高低缓存跨层级污染全 0）
# 代码更新时间：2026-08-06 22:41 RT_MONITOR_STATIC_VER 20260806-19（牛散出壳+缓存预热；宽基预热）
# 代码更新时间：2026-08-06 22:54 RT_MONITOR_STATIC_VER 20260806-20（财报切期：长TTL+保活+去重+前端防叠）
# 代码更新时间：2026-08-07 08:46 RT_MONITOR_STATIC_VER 20260807-1（期权T型：排除 name 末尾 A 调整合约）
# 代码更新时间：2026-08-07 10:54 RT_MONITOR_STATIC_VER 20260807-2（首页刷新配置默认轮询间隔 3→2 秒）
# 代码更新时间：2026-08-07 12:30 RT_MONITOR_STATIC_VER 20260807-3（opt-t-board 中金所股指期权 T型+价差+IV八块）
# 代码更新时间：2026-08-07 12:47 RT_MONITOR_STATIC_VER 20260807-4（IV 看板 ETF 显示名改为代码）
# 代码更新时间：2026-08-07 12:59 RT_MONITOR_STATIC_VER 20260807-5（IV 八块排序调整）
# 代码更新时间：2026-08-07 13:05 RT_MONITOR_STATIC_VER 20260807-6（中金所 IV/T 期限桶 6：下下月/下下季）
# 代码更新时间：2026-08-07 14:06 RT_MONITOR_STATIC_VER 20260807-7（申万新高低主表/明细一致）
# 代码更新时间：2026-08-07 17:11 RT_MONITOR_STATIC_VER 20260807-8（IV 看板 ETF 仅 4 档，中金所 6 档）
# 代码更新时间：2026-08-08 21:59 RT_MONITOR_STATIC_VER 20260808-1（主营构成近义/汇总行）
# 代码更新时间：2026-08-09 08:51 RT_MONITOR_STATIC_VER 20260809-1（opt-t-board 卖方扩展列）
# 代码更新时间：2026-08-09 09:05 RT_MONITOR_STATIC_VER 20260809-2（T型IV平价F+中债10Y；IV看板显示r）
# 代码更新时间：2026-08-09 10:05 RT_MONITOR_STATIC_VER 20260809-3（面板按日中债10Y全量重算）
# 代码更新时间：2026-08-09 10:46 RT_MONITOR_STATIC_VER 20260809-4（T型表禁横滚、表头不换行）
# 代码更新时间：2026-08-09 11:18 RT_MONITOR_STATIC_VER 20260809-5（T型：折溢%、去前收、涨跌红绿、行权价3位/虚实%2位着色）
# 代码更新时间：2026-08-09 11:33 RT_MONITOR_STATIC_VER 20260809-6（修复涨跌%/虚实%着色被 cell-num 默认色覆盖）
# 代码更新时间：2026-08-09 11:37 RT_MONITOR_STATIC_VER 20260809-7（T型与IV看板统一字号 token）
# 代码更新时间：2026-08-09 11:54 RT_MONITOR_STATIC_VER 20260809-8（时值允许负值+负时值着色）
# 代码更新时间：2026-08-09 12:13 RT_MONITOR_STATIC_VER 20260809-9（折溢%正青绿/负砖红着色）
# 代码更新时间：2026-08-09 12:56 RT_MONITOR_STATIC_VER 20260809-10（年化/IV 剩余天数对齐行情日）
# 代码更新时间：2026-08-09 15:13 RT_MONITOR_STATIC_VER 20260809-11（权利金%分边：Call备兑静态/Put净接货）
# 代码更新时间：2026-08-10 16:07 RT_MONITOR_STATIC_VER 20260810-1（去牛差熊差；增时值年化列）
# 代码更新时间：2026-08-10 16:10 RT_MONITOR_STATIC_VER 20260810-2（表头「时值年化」改「时年化」）
# 代码更新时间：2026-08-10 16:18 RT_MONITOR_STATIC_VER 20260810-3（去掉主列年化%，保留权利金%+时年化）
# 代码更新时间：2026-08-10 22:09 RT_MONITOR_STATIC_VER 20260810-4（权利金%/时年化核心列阶梯着色）
# 代码更新时间：2026-08-10 22:20 RT_MONITOR_STATIC_VER 20260810-5（权利金%/时年化分档阈值调整）
# 代码更新时间：2026-08-10 22:45 RT_MONITOR_STATIC_VER 20260810-6（去段头策略标签；最新价→收盘价；去涨跌%）
# 代码更新时间：2026-08-10 23:00 RT_MONITOR_STATIC_VER 20260810-7（T型增 Δ 与 IVR/分位）
# 代码更新时间：2026-08-10 23:39 RT_MONITOR_STATIC_VER 20260810-8（IVR Delta 展示门槛）
# 代码更新时间：2026-08-13 10:53 RT_MONITOR_STATIC_VER 20260813-2（历史信号可拖可缩弹窗）
# 代码更新时间：2026-08-13 12:54 RT_MONITOR_STATIC_VER 20260813-3（Sell Put 长假后首日禁开）
# 代码更新时间：2026-08-13 16:43 RT_MONITOR_STATIC_VER 20260813-4（588000 合槽四腿 2:2:3:3）
# 代码更新时间：2026-08-14 15:01 RT_MONITOR_STATIC_VER 20260814-1（Gap 锁当月+次月 1:1 次月钟）
# 代码更新时间：2026-08-14 21:29 RT_MONITOR_STATIC_VER 20260814-2（Sell Put 锁定 5/5/3/3 无替换）
# 代码更新时间：2026-08-14 22:20 RT_MONITOR_STATIC_VER 20260814-3（opt-t-board 壳页秒开+确认成交弹窗）
# 代码更新时间：2026-08-14 22:29 RT_MONITOR_STATIC_VER 20260814-4（Sell Put 计划区排版：规则卡片+状态条）
# 代码更新时间：2026-08-14 22:34 RT_MONITOR_STATIC_VER 20260814-5（确认成交透明可拖可缩；Sell Put 与 IV 右缘对齐）
# 代码更新时间：2026-08-14 22:37 RT_MONITOR_STATIC_VER 20260814-6（Sell Put 头信息单行）
# 代码更新时间：2026-08-14 22:41 RT_MONITOR_STATIC_VER 20260814-7（规则卡平仓改行内分隔，不再逐条换行）
# 代码更新时间：2026-08-14 22:45 RT_MONITOR_STATIC_VER 20260814-8（Gap/现货/名义占用同一行）
# 代码更新时间：2026-08-14 22:47 RT_MONITOR_STATIC_VER 20260814-9（现货醒目；下一交易日显示具体日期）
# 代码更新时间：2026-08-14 22:53 RT_MONITOR_STATIC_VER 20260814-10（开仓表合约月+剩余天数；出口改人话）
# 代码更新时间：2026-08-14 22:59 RT_MONITOR_STATIC_VER 20260814-11（头去结构说明；规则区次月钟+仓位期限 chip）
# 代码更新时间：2026-08-14 23:05 RT_MONITOR_STATIC_VER 20260814-12（开仓表单行；K/张去重；表头居中）
# 代码更新时间：2026-08-14 23:10 RT_MONITOR_STATIC_VER 20260814-13（Sell Put 每N万可调本金）
# 代码更新时间：2026-08-14 23:14 RT_MONITOR_STATIC_VER 20260814-14（规则区展示近一年回测关键数）
# 代码更新时间：2026-08-14 23:18 RT_MONITOR_STATIC_VER 20260814-15（Sell Put 表按内容定宽、表头居中）
# 代码更新时间：2026-08-14 23:26 RT_MONITOR_STATIC_VER 20260814-16（表铺满卡片：短列收紧、长列吃剩余）
# 代码更新时间：2026-08-14 23:36 RT_MONITOR_STATIC_VER 20260814-17（Sell Put 开仓改操作条、信号左右分栏）
# 代码更新时间：2026-08-14 23:41 RT_MONITOR_STATIC_VER 20260814-18（最近信号改卡片网格，去掉空洞表）
# 代码更新时间：2026-08-14 23:44 RT_MONITOR_STATIC_VER 20260814-19（信号卡 auto-fit；危险日不重复）
# 代码更新时间：2026-08-14 23:49 RT_MONITOR_STATIC_VER 20260814-20（开仓卡单行；规则标题与回测同行）
# 代码更新时间：2026-08-15 07:26 RT_MONITOR_STATIC_VER 20260815-1（开仓条字段组：合约/行权价K/名义本金）
# 代码更新时间：2026-08-15 07:48 RT_MONITOR_STATIC_VER 20260815-2（Sell Put 更正开仓）
# 代码更新时间：2026-08-15 07:56 RT_MONITOR_STATIC_VER 20260815-3（更正弹窗日期默认开仓日）
# 代码更新时间：2026-08-16 10:45 RT_MONITOR_STATIC_VER 20260816-1（Sell Put 通道 6日新低 chip）
# 代码更新时间：2026-08-16 11:26 RT_MONITOR_STATIC_VER 20260816-2（Sell Put Gap 4日新低 chip）
# 代码更新时间：2026-08-19 10:59 RT_MONITOR_STATIC_VER 20260819-1（510500 Sell Put 5/5/3/2）
# 代码更新时间：2026-08-19 21:22 RT_MONITOR_STATIC_VER 20260819-2（510500 合槽 3/6/5/2）
# 代码更新时间：2026-08-24 08:03 RT_MONITOR_STATIC_VER 20260824-1（IV 看板中轴 HV/Δ/HV Per）
# 代码更新时间：2026-08-24 08:31 RT_MONITOR_STATIC_VER 20260824-2（opt-t-board IV 下卖方读盘卡）
# 代码更新时间：2026-08-24 08:56 RT_MONITOR_STATIC_VER 20260824-3（中轴斜度；页头警示标明垂直价差）
# 代码更新时间：2026-08-24 20:38 RT_MONITOR_STATIC_VER 20260824-4（opt-t-board 叠 QMT 最新价）
# 代码更新时间：2026-08-24 21:12 RT_MONITOR_STATIC_VER 20260824-5（IV 看板五 ETF 叠 QMT 代表 IV）
# 代码更新时间：2026-08-25 07:45 RT_MONITOR_STATIC_VER 20260825-1（Sell Put 通道/Gap 盘中预演）
# 代码更新时间：2026-08-25 14:52 RT_MONITOR_STATIC_VER 20260825-2（T型拒僵报价；段头按新鲜度）
# 代码更新时间：2026-08-25 15:04 RT_MONITOR_STATIC_VER 20260825-3（T型成交量与 last 解耦，叠当日累加量）
# 代码更新时间：2026-08-25 15:19 RT_MONITOR_STATIC_VER 20260825-4（无成交不标实时；僵链叠 ETF）
# 代码更新时间：2026-08-25 17:36 RT_MONITOR_STATIC_VER 20260825-5（T型 last/vol/oi 对齐 QMT 日K）
# 代码更新时间：2026-08-25 18:18 RT_MONITOR_STATIC_VER 20260825-6（T型最新四位小数）
# 代码更新时间：2026-08-27 11:50 RT_MONITOR_STATIC_VER 20260827-1（opt-t-board SWR秒开+五标的切换缓存）
# 代码更新时间：2026-08-27 13:22 RT_MONITOR_STATIC_VER 20260827-2（Sell Put 并行秒开+新月 tick 跟盘）
# 代码更新时间：2026-08-27 14:10 RT_MONITOR_STATIC_VER 20260827-3（T型/IV 304+HTML缓存，减轻10月扩链卡顿）
# 代码更新时间：2026-08-27 14:20 RT_MONITOR_STATIC_VER 20260827-4（IV/预演 SWR+前端防叠，去掉 25s/17s 堵轮询）
# 代码更新时间：2026-08-28 06:57 RT_MONITOR_STATIC_VER 20260828-1（T型/IV 轮询对齐 TTL 8s，去 3s 空打）
# 代码更新时间：2026-08-28 07:08 RT_MONITOR_STATIC_VER 20260828-2（直播 TTL/轮询 4s，盘后 10s）
# 代码更新时间：2026-08-28 08:04 RT_MONITOR_STATIC_VER 20260828-3（T型展示全部合约含 A/非10000乘数）
# 代码更新时间：2026-08-29 15:16 RT_MONITOR_STATIC_VER 20260829-1（T型到期模块间距 1px）
# 代码更新时间：2026-08-29 15:22 RT_MONITOR_STATIC_VER 20260829-2（表头/SellPut/IV 模块间距 1px）
# 代码更新时间：2026-09-02 07:49 RT_MONITOR_STATIC_VER 20260902-1（159915 Sell Put 3/5/3/3）
# 代码更新时间：2026-09-02 09:28 RT_MONITOR_STATIC_VER 20260902-2（申万页过滤盘前-100%、首屏不堵新高低）
# 代码更新时间：2026-09-02 09:48 RT_MONITOR_STATIC_VER 20260902-3（申万页首屏缩短日线窗+冷路径 singleflight）
# 代码更新时间：2026-09-02 09:57 RT_MONITOR_STATIC_VER 20260902-4（申万页首屏先行情后财报）
# 代码更新时间：2026-09-02 10:50 RT_MONITOR_STATIC_VER 20260902-5（Top5%_pct 弹窗滚动2σ O(n)）
# 代码更新时间：2026-09-02 14:04 RT_MONITOR_STATIC_VER 20260902-6（去掉 588000 页头「强信号再做」警示）
# 代码更新时间：2026-09-07 15:01 RT_MONITOR_STATIC_VER 20260907-1（top_moves 8s缓存+singleflight；隐藏页不轮询）
# 代码更新时间：2026-09-07 17:27 RT_MONITOR_STATIC_VER 20260907-2（首页轮询隐藏页全停；切回立即刷）
# 代码更新时间：2026-09-07 18:19 RT_MONITOR_STATIC_VER 20260907-3（申万页轮询 2s；实时层 TTL 10s）
# 代码更新时间：2026-09-07 18:27 RT_MONITOR_STATIC_VER 20260907-4（申万涨跌幅贴近 2s；分位 30s）
# 代码更新时间：2026-09-07 18:38 RT_MONITOR_STATIC_VER 20260907-5（首页/涨跌家数默认 1s）
# 代码更新时间：2026-09-07 18:40 RT_MONITOR_STATIC_VER 20260907-6（强制首页默认间隔 1s，清 autofill 旧 2）
# 代码更新时间：2026-09-07 20:58 RT_MONITOR_STATIC_VER 20260907-7（opt-t-board 顶栏 SMA120 动量闸状态灯）
# 代码更新时间：2026-09-08 22:25 RT_MONITOR_STATIC_VER 20260908-1（SMA120 灯去掉已过/未过字样）
# 代码更新时间：2026-09-08 22:31 RT_MONITOR_STATIC_VER 20260908-2（五只 ETF chip 各自挂 SMA120）
# 代码更新时间：2026-09-09 06:38 RT_MONITOR_STATIC_VER 20260909-1（chip 去#序；固定 588000→…→510500）
# 代码更新时间：2026-09-09 06:43 RT_MONITOR_STATIC_VER 20260909-2（隐藏顶栏弹性排序说明）
# 代码更新时间：2026-09-09 07:28 RT_MONITOR_STATIC_VER 20260909-3（中证2000/北证50 拼 rt_idx_k）
# 代码更新时间：2026-09-09 16:49 RT_MONITOR_STATIC_VER 20260909-4（T型卖价买一/折溢ask/TDX段头）
# 代码更新时间：2026-09-09 17:49 RT_MONITOR_STATIC_VER 20260909-5（时值&lt;0 全套买方ask口径）
# 代码更新时间：2026-09-09 18:01 RT_MONITOR_STATIC_VER 20260909-6（列名恢复最新；与评估价分离）
# 代码更新时间：2026-09-09 18:04 RT_MONITOR_STATIC_VER 20260909-7（仅卖一时值&lt;0 才买方评估）
# 代码更新时间：2026-09-09 18:13 RT_MONITOR_STATIC_VER 20260909-8（时值/有效价四位；时年化两位）
# 代码更新时间：2026-09-10 14:05 RT_MONITOR_STATIC_VER 20260910-1（T型直播 0.8s）
# 代码更新时间：2026-09-10 15:20 RT_MONITOR_STATIC_VER 20260910-2（T型按标的内容缓存）
# 代码更新时间：2026-09-20 21:26 RT_MONITOR_STATIC_VER 20260920-1（Per 1y/C/A 三列 + 锚点配置）
# 代码更新时间：2026-09-20 21:51 RT_MONITOR_STATIC_VER 20260920-2（分位列去 ☆）
# 代码更新时间：2026-09-21 06:12 RT_MONITOR_STATIC_VER 20260920-3（优选 mid 底/行权价去星/量仓万单位）
# 代码更新时间：2026-09-21 06:22 RT_MONITOR_STATIC_VER 20260921-1（平值橙底不盖量仓 top3）
# 代码更新时间：2026-09-21 06:31 RT_MONITOR_STATIC_VER 20260921-2（T 板顶栏 Per C 锚点入口）
# 代码更新时间：2026-09-21 06:54 RT_MONITOR_STATIC_VER 20260921-3（T/义务仓加宽、IV 分位列收紧）
# 代码更新时间：2026-09-21 07:03 RT_MONITOR_STATIC_VER 20260921-4（IV ≥100% 一位小数）
# 代码更新时间：2026-09-21 07:12 RT_MONITOR_STATIC_VER 20260921-5（T 型 IV 取整让宽；义务仓 ┊ 居中）
# 代码更新时间：2026-09-21 07:22 RT_MONITOR_STATIC_VER 20260921-6（撤销 3360 断行，桌面左右并排）
# 代码更新时间：2026-09-21 07:34 RT_MONITOR_STATIC_VER 20260921-7（义务仓单行表头、不横滚）
# 代码更新时间：2026-09-21 07:41 RT_MONITOR_STATIC_VER 20260921-8（表头整段居中不换行）
# 代码更新时间：2026-09-21 07:45 RT_MONITOR_STATIC_VER 20260921-9（档位列加宽；字号同 T 型）
# 代码更新时间：2026-09-21 07:52 RT_MONITOR_STATIC_VER 20260921-10（IV 右栏给足宽度、禁横条）
# 代码更新时间：2026-09-21 08:07 RT_MONITOR_STATIC_VER 20260921-11（义务仓 Per 间距用留白）
# 代码更新时间：2026-09-21 08:15 RT_MONITOR_STATIC_VER 20260921-12（☆/┊ 不重叠；档位右留白收）
# 代码更新时间：2026-09-21 08:25 RT_MONITOR_STATIC_VER 20260921-13（义务仓 ┊ 对齐 + 期限底色）
# 代码更新时间：2026-09-21 08:35 RT_MONITOR_STATIC_VER 20260921-14（左侧贴数字，余量给 Per+☆）
# 代码更新时间：2026-09-21 08:47 RT_MONITOR_STATIC_VER 20260921-15（义务仓平值行琥珀金加描边）
# 代码更新时间：2026-09-21 10:07 RT_MONITOR_STATIC_VER 20260921-16（T板墙变SWR+壳页热缓存秒开）
# 代码更新时间：2026-09-21 10:20 RT_MONITOR_STATIC_VER 20260921-17（申万 partial 缓存+SWR秒开）
# 代码更新时间：2026-09-21 11:03 RT_MONITOR_STATIC_VER 20260921-18（空amt穿透+fina版本重绘）
# 代码更新时间：2026-09-21 11:15 RT_MONITOR_STATIC_VER 20260921-19（_full勿返pending；补齐提示）
# 代码更新时间：2026-09-21 11:20 RT_MONITOR_STATIC_VER 20260921-20（pending不跳过预热）
# 代码更新时间：2026-09-23 06:46 T 表 Γ 两位 + STATIC_VER 20260923-14
# 代码更新时间：2026-09-23 06:54 IV 看板表头改「溢」+ STATIC_VER 20260923-15
# 代码更新时间：2026-09-23 07:09 chip Intra + STATIC_VER 20260923-16
# 代码更新时间：2026-09-23 07:21 顶栏 1px 间距 + STATIC_VER 20260923-17
# 代码更新时间：2026-09-23 07:36 义务仓/权利仓竖缝 1px + STATIC_VER 20260923-18
# 代码更新时间：2026-09-23 07:53 Sell Put 首屏自启动 + STATIC_VER 20260923-19
# 代码更新时间：2026-10-01 12:45 RT_MONITOR_STATIC_VER 20261001-1（成交额表全历史 MAX / 峰值日）
# 代码更新时间：2026-10-04 21:22 RT_MONITOR_STATIC_VER 20261004-1：市场状态页数据体检摘要
```
