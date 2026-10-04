# 本轮完整生产包性能对照方法

任务日期为 2026-10-04；四次完整测量与本报告于 2026-10-05（Asia/Shanghai）完成。

比较入口是 [roadmap_performance_probe.py](../../lab/roadmap_performance_probe.py)。它比较固定提交 `a465356d4363943af461d90c6199e0d31217ee10` 与当前候选的完整原生 payload、管理工具和实际 systemd unit，不沿用早期只替换 ELF 的对照方式。生产控制器没有实验命令覆盖，根服务与数据盘父 slice 分别保持原始 20% / 20 ms 配额，两个预算合计为单核 40%；实验读取实际 cgroup、systemd 属性、进程 capability/NoNewPrivs/seccomp 和挂载信息进行记录。

双方使用同一完整 Ubuntu 种子、内核、通用救援 base、宿主冻结工具库和逐字节一致的 RAM 观察器。旧版 builder 中固定读取“本机已安装救援包”的输入，仅在独立准备进程里重定向到同一显式通用 base；旧版运行时源码、session、manager、unit 不改。该设计控制基包差异，测量本轮运行路径与权限策略的成本；不把它描述为两套不同发行版或既往实机已安装工具库的性能比较。临时 Git archive 只用于本次历史 A/B，当前版本的干净复现入口不依赖旧提交。

RAM 日志服务同样使用各自版本未修改的 unit，检查实际属性、文件散列、无额外 drop-in 及恢复前后存活；旧版不会被强加新版的 `CAP_SYSLOG`/`ProtectSystem=strict`。日志服务属于独立 `ramrescue.slice`，不计入本文 Guard 根服务加数据盘 slice 的 CPU/PSS 汇总，两个版本均保留同样的计费范围。

## 执行

先完成源码修改，停止其他 QEMU/性能实验，再准备材料；默认不启动 VM：

```bash
python3 lab/roadmap_performance_probe.py \
  --build-dir lab/work/protected-rm4-1004 \
  --enrollment lab/work/rp-1004c/enrollment.json \
  --seed-report lab/work/rp-1004c/seed/report.json \
  --binary lab/work/protected-rm4-1004/native-runtime/guard-runtime \
  --base-rescue-dir lab/work/base-prov2-1004
```

将输出的 `prepared.json` 传入显式运行命令：

```bash
python3 lab/roadmap_performance_probe.py \
  --prepared lab/work/rperf-实际输出目录/prepared.json --run --trials 2
```

默认每个版本两次，以基线/候选、候选/基线顺序串行执行，使用全新写入 overlay 和可丢弃数据盘。准备和运行都拒绝 root；沿用现有仅允许实验目录普通镜像的输入检查，没有网络、共享目录或宿主块设备透传。材料、源码、seed 或 native ELF 变化会拒绝沿用旧准备结果。

## 口径

| 项目 | 采集方式 | 限制 |
| --- | --- | --- |
| CPU 均值与峰值 | 每 20 ms 读取 cgroup 累计 CPU，以实际间隔换算；另计算约 100 ms 峰值 | 单个逻辑核为 100%；跨配额周期的采样峰值不等于单周期硬上限，亦非任意瞬间峰值 |
| 常态与事件风暴 | 各 20 秒 idle、不相关事件、相关事件；风暴为每 100 ms 十个实际 kernel uevent | 观察器在 Guard cgroup 外，但仍消耗 VM CPU；两边使用同一采样实现 |
| 三映射恢复 | 根盘、ext4、FAT 一起断联，删除事件后约 0.2 秒再连接；16 秒采样，重接后 17 秒静默再读取结果 | 排队、内核枚举与调度使实际空窗不一定精确为 0.2 秒，以 QMP 记录为准 |
| 内存/PSS | 20 ms `memory.current`；200 ms smaps_rollup/PSS；阶段前后保存 `memory.peak` 等完整快照 | cgroup peak 是服务生命周期值；PSS/内存采样峰值是阶段窗口观察值，不能相加当作物理总内存 |
| 子进程开销 | cgroup CPU 自带 helper 计费；20 ms helper PID+start_ticks 和并发量；Guard 的累计 waited-child CPU ticks | 很短 helper 会在两次采样间结束，出现次数是下界；CPU ticks 有量化，不能精确分解全部短 helper 时间 |
| 汇总 | 根服务与 data 父 slice 各计一次；原始每映射数据仍保留 | 不把父 slice 和它的两个子服务重复相加；sampler、业务负载、内核 worker 不包含在 Guard cgroup 数字中 |
| 恢复时延 | 事务 `waiting`→`ready`、QMP 断联空窗、应用 fsync/直接读写最长耗时 | 时钟来源独立标注；没有把 host monotonic 与 guest monotonic 直接相减 |

每轮仍验收相同 Guard/应用进程、原挂载、原 owner、恰好一次恢复、持久数据一致性、正常关机及离线 fsck。任何一轮失败均保存 report、QMP、串口日志和镜像，不计入成功汇总，不继续自动补跑以掩盖失败。汇总报告包含每个原始报告的散列。

方法通过 9 项专用单元检查及原有 4 项资源/静默窗口检查；最终 AB/BA 四个 VM 均通过 26 条验收，共 104 条。具体测量结果如下，未据此宣称全面降耗。

## 观察器准备时发现的问题

第一份准备材料 `lab/work/rperf-1004-231857-611723/` 的 baseline 第 0 场在采样前失败：三个实际 owner 已 ready，但新启动的 quota 查询 RPC 缺少管理模块 import path，报 `ModuleNotFoundError: native_payload`。替换 mount 观察器时漏掉了它原有的初始化路径；已只在双方共用的性能观察器中显式补上 `/run/data-launcher-src/guard`。原失败报告与 comparison.json 保留，没有用它提取任何资源改善数字。

修正后材料为 `lab/work/rperf-1004-233620-677099/prepared.json`，baseline 和 current 继续使用各自完整生产包及未经实验覆盖的 unit，原生 ELF 和生产源码没有为性能试验调整。

## 四次完整测量结果

公开结构化报告：[roadmap-performance.json](../../lab/results/2026-10-04-roadmap-performance.json)。原始报告为 `lab/work/rperf-1004-233620-677099/{b0,c0,c1,b1}/report.json`，顺序为基线/当前/当前/基线；`comparison.json` 保存汇总与每份原始报告散列。四场都通过原生 owner、实际生产 unit/配额、原进程/原挂载、三映射恢复、数据校验、正常关机与离线 fsck。

环境为 KVM、2 vCPU、3072 MiB RAM、同一 Ubuntu 种子及 `7.0.0-34-generic` 内核。current 指本次冻结完整包 `25b76b7`；77 项输入、unit 和 ELF 散列以 prepared.json 为准。后续冷态 doctor 的 removed 生命周期与 next-boot 展示修正，以及干净复现结果串口调整，没有参与本轮测量；原生运行时、服务策略和所测健康/恢复路径未改变。

下表 CPU 单位均为单个逻辑核百分比，汇总三个 controller 和其 helper，根服务与数据父 slice 各计一次。均值列取两次试验均值的中位数，峰值列取两次试验观察到的最大值；采样窗口约 20 ms 和 100 ms。

| 阶段 | 基线：均值 / 20 ms 峰 / 100 ms 峰 | 当前：均值 / 20 ms 峰 / 100 ms 峰 |
| --- | ---: | ---: |
| 常态 | 0.06994% / 2.49% / 0.50% | 0.07678% / 2.92% / 0.78% |
| 无关事件风暴 | 0.28105% / 3.59% / 0.97% | 0.28755% / 3.86% / 1.43% |
| 相关事件风暴 | 0.43419% / 4.28% / 1.22% | 0.46314% / 5.12% / 1.16% |
| 恢复（完整 16 s 窗口） | 0.58698% / 36.66% / 17.61% | 0.62574% / 32.34% / 15.58% |

常态均值的两次范围为基线 0.06119–0.07868%，当前 0.07389–0.07967%，存在重叠。本轮常态观察中位数略高，不能宣称安全加固降低了常态 CPU。当前最忙恢复窗口的 20 ms 峰值为 32.34%，100 ms 峰值为 15.58%；根服务与数据 slice 的两个配额合计为单核 40%，这些采样值不是任意瞬间上限。

下表内存均为 MiB，取两次最大采样值。cgroup 的物理页计费与进程 PSS 分摊机制不同，数值不能相加；它们也不等于整个 RAM 救援工具镜像的占用。

| 阶段 | 基线：cgroup 采样峰 / PSS 采样峰 | 当前：cgroup 采样峰 / PSS 采样峰 |
| --- | ---: | ---: |
| 常态 | 5.68 / 9.56 | 6.41 / 9.73 |
| 无关事件风暴 | 6.16 / 9.58 | 6.41 / 9.74 |
| 相关事件风暴 | 6.16 / 9.58 | 6.40 / 9.72 |
| 恢复（完整 16 s 窗口） | 8.85 / 10.51 | 8.61 / 17.74 |

当前恢复 PSS 的两次峰值样本都包含三个 controller 加一个短命 helper；基线的最大样本均只含三个 controller。报告保留峰值时每个 PID/start_ticks/PSS，不能用 200 ms 采样证明基线短命 helper 的真实瞬时峰值更小。当前观测到的 17.74 MiB 峰值仍如实计入；常态 PSS 峰值则约从 9.56 增至 9.73 MiB。

生命周期 `memory.peak` 单独保留：基线 root 为 5.63–5.67 MiB，数据父 slice 为 6.70–7.21 MiB；当前 root 为 5.56–5.70 MiB，数据父 slice 为 6.85–6.86 MiB。这些高水位发生时刻不一定相同，没有将它们相加当成同时占用。四场均无 OOM 或 swap 计费。

常态及两种风暴阶段没有采样到 helper，waited-child CPU ticks 为 0；恢复阶段采样到基线 18–20、当前 19–20 个 helper 实例（下界），单个 controller 观察到的 helper 并发峰值为 1。USER_HZ=100 时，累计已等待 child CPU 为基线 30–40 ms、当前 30 ms；总恢复 cgroup CPU 已含这些成本，不能再次相加。

## 恢复时延与业务观察

本轮虽然设置了删除后等待约 0.2 秒再重接，实际 QMP 删除事件到连接回复的空窗约为根盘 0.463 秒、ext4 0.523 秒、FAT 0.482 秒。不能把这些实验称为实际断联仅 0.2 秒。

| 指标 | 基线两次范围 | 当前两次范围 |
| --- | ---: | ---: |
| 根 Guard waiting→ready | 1.408–1.438 s | 1.412–1.432 s |
| 三映射最早 waiting→全部 ready | 2.469–3.241 s | 3.255–3.308 s |
| 上述活动恢复窗口的总 cgroup CPU | 81.35–89.08 ms | 90.71–90.86 ms |
| 与故障窗口重叠的根盘 fsync 最长耗时 | 1.335–1.369 s | 1.349–1.361 s |
| 与故障窗口重叠的 ext4 写入/直接读最长耗时 | 2.304–2.313 s | 2.328–2.348 s |
| 与故障窗口重叠的 FAT 写入/直接读最长耗时 | 2.297–3.065 s | 3.083–3.139 s |

业务耗时从同一 guest monotonic 时钟的记录中选取：请求开始不晚于该映射 ready，且完成不早于 waiting。全流程最大值另保留在 JSON，基线最高约 6.21 s、当前最高约 6.75 s，其中包含恢复后的取证/审计阶段，不能混称为故障恢复时延。所有被确认写入的前缀/校验和一致，没有应用 I/O 错误，原挂载与原应用进程保持。

每个版本只有两次完整试验，不能据此给出统计置信区间或实机最大时延保证。本轮结论是新安全限制下功能与有限资源预算得到实际验收；常态 CPU 略增、恢复活动 CPU/全映射时延也有增加，尚无证据支持“全面性能改善”。
