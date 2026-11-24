# 深度登记 烟道排放复算台

- 规模：中型
- 核心规则：干基、统一参比氧与固定小时门槛分开计算，以分钟裁决维持应测分钟守恒。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 分钟原件采信 | 检查原件、重复表头与坏行 | `GET /api/samples/dirty/intake` | `app/monitor/reader.py` `app/monitor/source.py` | `tests/test_intake.py` |
| M2 干基与参比氧折算 | 核对修订、单位和折算值 | `GET /api/samples/dirty/oxygen` | `app/monitor/oxygen.py` `app/monitor/revision.py` | `tests/test_oxygen.py` |
| M3 分钟有效性裁决 | 按分钟查看有效、免计与缺证 | `GET /api/samples/dirty/validity` | `app/monitor/validity.py` `app/monitor/modes.py` | `tests/test_validity.py` |
| M4 小时汇总复算 | 比对有效分钟门槛和小时均值 | `GET /api/samples/dirty/hours` | `app/monitor/hours.py` | `tests/test_hours.py` |
| M5 报送差异对账 | 核对差值、单据和游离报送 | `GET /api/samples/dirty/reports` | `app/monitor/reconcile.py` `app/monitor/reports.py` | `tests/test_reports.py` |
| M6 批次留档复跑 | 汇总归档、并发复跑和查看历史 | `POST /api/runs` | `app/archive/run.py` `app/archive/store.py` | `tests/test_runs.py` |
| M7 监测证据签发 | 下载和校验原件、摘要和判据 | `GET /api/export/{id}` | `app/archive/certificate.py` | `tests/test_evidence.py` |
| M8 超限事件交接 | 小时复算页交接待研判/确证事件与处置单 | `GET /api/samples/dirty/exceedance` | `app/monitor/exceedance.py` `app/archive/handoff.py` | `tests/test_exceedance.py` `tests/test_handoff.py` |

## 陷阱登记

| TRAP | 原件现象 | 直接处理的误差 | 处理 | 位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 分钟 CSV 首列 BOM | 识别失败 | 清除首位 BOM | `app/monitor/reader.py` | `tests/test_intake.py::test_bom` |
| TRAP-04 | 分段重复表头 | 当作采样 | 分段计数 | `app/monitor/reader.py` | `tests/test_intake.py::test_repeated_header` |
| TRAP-05 | 短列尾残行 | 列对应错位 | 隔离原值和起始行 | `app/monitor/reader.py` | `tests/test_intake.py::test_bad_columns` |
| TRAP-07 | 浓度 NULL | 被按零计 | 缺证并保留原值 | `app/numbers.py` | `tests/test_oxygen.py::test_sentinel` |
| TRAP-10 | 逐分钟舍入与小数平均 | 先舍入偏移均值 | 完整精度平均后量化 | `app/monitor/hours.py` | `tests/test_hours.py::test_decimal_hour` `tests/test_hours.py::test_unrounded_minutes` |
| TRAP-11 | UTC 与正八区同分钟 | 重复时间槽 | 统一到 UTC | `app/monitor/clock.py` | `tests/test_validity.py::test_shared_zone_conversion` |
| TRAP-13 | 晚收修订 | 原件顺序改答案 | 截止前迟补计入并标识 | `app/monitor/revision.py` | `tests/test_oxygen.py::test_version_and_cutoff` |
| TRAP-14 | 同版测量冲突 | 后写覆盖 | 两条保留在隔离证据 | `app/monitor/revision.py` | `tests/test_oxygen.py::test_true_conflict_does_not_pick_last` |
| TRAP-15 | mg、g、µg 混测 | 数量级错误 | 单位系数归一 | `app/monitor/oxygen.py` | `tests/test_oxygen.py::test_units` |
| TRAP-19 | 启停工况重叠 | 重复免计或顺序覆盖 | 并集与停机优先 | `app/monitor/modes.py` | `tests/test_validity.py::test_overlap_priority` |
| TRAP-23 | 末端分钟 | 多免计一分钟 | 半开区间 | `app/monitor/modes.py` | `tests/test_validity.py::test_half_open` |
| TRAP-26 | 高值段中间缺一分钟 | 缺测即判超限造成误报 | 缺口挂 PENDING，补齐前不出确证 | `app/monitor/exceedance.py` | `tests/test_exceedance.py::test_single_missing_minute_between_high_values_is_pending_not_confirmed` |
| TRAP-27 | 跨整点连续高值 | 按小时切断漏判 | 整窗分钟序列扫描接续 | `app/monitor/exceedance.py` | `tests/test_exceedance.py::test_high_run_carries_across_the_hour_boundary` |
| TRAP-28 | 停机免计夹在高值之间 | 缺口跨过免计分钟接续 | 免计分钟硬断开高值段 | `app/monitor/exceedance.py` | `tests/test_exceedance.py::test_shutdown_exemption_breaks_the_chain` |
| TRAP-29 | 两班同时提交同时段补证 | 同一事件建出多张处置单 | 事件键唯一 UPSERT，单事务串行 | `app/archive/handoff.py` `app/archive/store.py` | `tests/test_handoff.py::test_two_shifts_submitting_same_window_keep_single_ticket` |
| TRAP-30 | 晚到记录改写历史结论 | 旧单状态被覆盖丢失 | 追加结论轨迹，归档快照冻结 | `app/archive/handoff.py` | `tests/test_handoff.py::test_pending_then_confirmed_after_late_record_fills_gap` |

## 对账规则

- 污样分钟源 109 条记录 = 可读 107 + 重复表头 1 + 结构隔离 1。
- 120 个应测分钟 = 91 有效 + 8 工况免计 + 21 缺证。参比氧复算与报送按小时核对。
- 内容摘要与规则版本组成留档键，原件和计算结果同事务存储，重复提交复用原清单。
- 超限处置单以「烟道 + 窗口起点 + 首高值分钟」为事件键，与运行包在同一事务对账：
  新证据降级为 `REVOKED`、事件消失为 `WITHDRAWN`，历史轨迹只追加不改写；处置单数量按
  事件键唯一，两班并发提交经 `BEGIN IMMEDIATE` 串行后仍只保留一张当前单。

## 已知边界

输入接受 UTF-8 原件包。监测窗口最多 24 小时，数据包限长以控制内存。不推断本地无偏移时间或将分钟平均换算成质量排放。
