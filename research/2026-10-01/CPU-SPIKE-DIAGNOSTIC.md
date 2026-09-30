# 恢复 CPU 短窗口峰值调查

结论：保留原先 **158.446% / 21.818 ms** 的异常计账样本，不能把它删掉，也不能据此断言 Guard 的用户代码连续消耗了 1.58 个核心。新增的六轮故障实验没有复现该数值，但实际观测到一次串口硬中断时间被计入当时运行的 `lvm` 子进程，进而进入 Guard 的 cgroup CPU 计数。原异常的具体原因仍未确定；当前 `CPUQuota=20%` 也不是每个任意短窗口都严格不超过 20% 的保证。

## 原样本与不能倒推的结论

原报告保存在 `lab/work/perf-c20-1001-045728-742669/report.json`：

| 项目 | 记录 |
|---|---:|
| guest monotonic 区间 | 86.165377861 → 86.187196037 s |
| 间隔 | 21.818176 ms |
| ext4 服务 `usage_usec` 增量 | 34,571 µs |
| 父 data slice 增量 | 34,570 µs |
| root / vfat 增量 | 0 / 0 µs |
| 汇总短窗口比例，单核为 100% | 158.445876% |
| 100 ms 窗口峰值 | 48.546120% |
| 父 `cpu.max` | `4000 20000` |
| `nr_periods` 增量 / `nr_bursts` | 2 / 0 |

父 slice 与叶服务是同一批工作，两者没有相加。其后约 160 ms，ext4 CPU 累计值不变，同时父 slice 的节流周期增加；这与超额计账后偿还配额的形态相符，尚不能作为该机制已经在原样本中被证明的证据。

原采样器在依次读取 root、ext4、vfat、父 slice 前只记录一次时间，没有每次 `cpu.stat` 读取的结束时间，也没有调度、中断跟踪。因此它无法排除读取延迟、运行时间延后入账、IRQ 归属或虚拟机调度影响。`user_usec` 与 `system_usec` 由累计时间校正，不能把这个短窗口的差额直接解释成真实用户态/内核态指令占比。

## 新增的独立实验

工具为 `lab/cpu_diagnostic.py`、`lab/guest/cpu_diagnostic.py`、`lab/analyze_cpu_diagnostic.py`。它们复用已有隔离 Ubuntu VM 的引导与可丢弃磁盘夹具，没有修改生产 Guard 或原性能采样器。两核 KVM、Ubuntu `7.0.0-34-generic`，root Guard 和 data 总 slice 分别设置 `4000 20000`；所有故障都同时断开并重连 root、ext4、vfat 三盘。

命令：

```sh
python3 lab/cpu_diagnostic.py --rounds 6
python3 lab/analyze_cpu_diagnostic.py lab/work/cpudiag-1001-052303-876020
```

六轮全部通过：原进程、挂载和已确认数据保留，零工作负载 I/O 错误，正常关机，离线文件系统检查通过，源镜像与实验源文件哈希不变。报告为 `lab/work/cpudiag-1001-052303-876020/report.json`，原始证据为同目录 `accounting.json.gz` 和 `trace.txt.gz`。跟踪器记录了 347,736 个事件；两 CPU 的 overrun、commit overrun、dropped events 均为零，调度切换连续性错误为零。

分析器的三项独立样例测试也通过，覆盖真实 fork 事件字段、未被 `/proc` 采到的短命后代、嵌套 IRQ 时间并集、runtime 与调度策略事件、父组不重复累加子组，以及丢失调度切换的检测，见 `lab/tests/test_cpu_diagnostic.py`。

额外观测包括每次 `cpu.stat` 读取前后时间、`sched_switch`、`sched_stat_runtime`、fork/exec/exit、cgroup 移动、调度策略修改系统调用、ioctl、IRQ/softirq 以及 `throttle_cfs_rq_work` 回调。沿 fork 追踪 302 个 Guard 及其后代任务；所有观测到的调度优先级均为 120，抽样策略均为 `SCHED_OTHER`，没有观测到策略修改调用或离开原 cgroup 的任务。这些观测支持本次子进程受同一预算管理，不是实时调度类绕过预算。

| 独立诊断中各组最高读数窗口 | `cpu.stat` 增量 | 读取中点间隔 | 单核百分比 | 调度驻留时间 | 重叠的已跟踪 IRQ/softirq |
|---|---:|---:|---:|---:|---:|
| root | 30,557 µs | 54.125 ms | 56.456% | 30,571 µs | 26,787 µs |
| ext4 | 9,266 µs | 20.052 ms | 46.209% | 9,268 µs | 18 µs |
| vfat | 8,732 µs | 19.750 ms | 44.213% | 8,731 µs | 19 µs |
| data 总 slice | 9,372 µs | 20.161 ms | 46.485% | 9,372 µs | 18 µs |

这些窗口是各自独立的最大值，不能相加。诊断跟踪本身改变时序和负载，表内数字不用于替代无跟踪性能基线。

root 的异常窗口可具体定位：CPU0 当前任务为 PID 1770 的 `lvm`，`ttyS0` IRQ4 从 **28.085944 s 到 28.112207 s**，持续 **26.263 ms**。随后在 28.112213 s，`sched_stat_runtime` 为这个 `lvm` 记录 **26,764,019 ns**，该窗口 root 的全部 runtime 事件合计 **30,556.780 µs**，与 `cpu.stat` 的 **30,557 µs** 相符。两次 `cpu.stat` 读取分别仅 72.170 µs、120.886 µs，所以本窗口的 30 ms 跃升不能由这两次文件读取延迟解释。

这证明本次计账确实含有发生在该任务上下文中的长 IRQ 驻留，**不证明中断的全部墙钟时间都在宿主物理 CPU 上执行**，也不证明原 ext4 158% 样本由同一个 IRQ 引起。`sched_switch` 驻留本身包括中断以及可能的宿主调度停顿；即使减去跟踪到的中断，剩余值也只是对任务自身 CPU 执行的上界。

这里的 `ttyS0` 是串口中断，不能改称 USB 中断。实验观察器本身通过虚拟串口传输 RPC 和结果；观察器进程位于 Guard cgroup 之外，也不意味着其串口中断绝不会记到当时正在运行的 Guard 子进程。这是本实验测量通道的额外边界。

## Linux 7 与 LVM 的源码边界

以下使用官方 Linux `v7.0`、LVM `v2_03_16` 原始源码。行号按本次保存的对应原始文件，链接固定到版本。

1. [`kernel/sched/core.c` 786–836](https://github.com/torvalds/linux/blob/v7.0/kernel/sched/core.c#L786) 的 `update_rq_clock_task` 只有在启用相应配置时扣除精确 IRQ 时间或 paravirtual steal。当前同版本 Ubuntu 配置 `/boot/config-7.0.0-34-generic` 明确未启用 `CONFIG_IRQ_TIME_ACCOUNTING` 和 `CONFIG_PARAVIRT_TIME_ACCOUNTING`。这提供了本次 IRQ 时间进入任务运行时间的源码解释；不应把当前构建的 cgroup CPU 数字描述成完全剔除了中断与虚拟化调度影响的纯 Guard 指令时间。
2. [`kernel/sched/fair.c` 1238–1263](https://github.com/torvalds/linux/blob/v7.0/kernel/sched/fair.c#L1238) 从 `rq_clock_task` 差值产生 `sched_stat_runtime` 并更新 cgroup 计账。两个计数相符不是两套相互独立的物理 CPU 测量；本次另用 `sched_switch` 和 IRQ 事件检查时间归属。
3. [`fair.c` 5719–5736](https://github.com/torvalds/linux/blob/v7.0/kernel/sched/fair.c#L5719) 先扣运行额度，额度可为负；[5952–5964](https://github.com/torvalds/linux/blob/v7.0/kernel/sched/fair.c#L5952) 将节流工作安排为 `TWA_RESUME`，由 [5773–5816](https://github.com/torvalds/linux/blob/v7.0/kernel/sched/fair.c#L5773) 在任务上下文执行实际出队；[6048–6058](https://github.com/torvalds/linux/blob/v7.0/kernel/sched/fair.c#L6048) 处理负额度偿还。官方引入提交为 [task based throttle model](https://github.com/torvalds/linux/commit/e1fad12dcb66b7f35573c52b665830a1538f9886)。因此配额不是任意内核工作执行中都能立刻截断的硬实时上限。本次观察到 209 次该节流回调；没有反向据此声称已确定原峰值的精确路径。
4. [`kernel/cgroup/rstat.c` 388–433、713–734](https://github.com/torvalds/linux/blob/v7.0/kernel/cgroup/rstat.c#L388) 在读 `cpu.stat` 时汇总各 CPU 的统计，该刷新可能阻塞，用户/系统时间另经校正；所以新诊断对每一次读取单独记时间。
5. [cgroup v2 文档 1138–1163](https://github.com/torvalds/linux/blob/v7.0/Documentation/admin-guide/cgroup-v2.rst#L1138) 区分所有任务的 CPU 统计与 fair 调度类带宽控制；[CFS bandwidth 文档](https://github.com/torvalds/linux/blob/v7.0/Documentation/scheduler/sched-bwc.rst#L159) 还说明 CPU 本地剩余额度、短时突发与长期约束。因此 `nr_bursts=0` 不能证明每个 20 ms 采样窗都严格受 20% 限制。
6. LVM [`lib/mm/memlock.c` 481–509](https://github.com/lvmteam/lvm2/blob/v2_03_16/lib/mm/memlock.c#L481) 的优先级提升是 `setpriority` 调整 nice。对本次涉及的 [dmsetup](https://github.com/lvmteam/lvm2/blob/v2_03_16/libdm/dm-tools/dmsetup.c)、[libdevmapper ioctl 实现](https://github.com/lvmteam/lvm2/blob/v2_03_16/libdm/ioctl/libdm-iface.c) 未找到设置 `SCHED_FIFO/RR` 的调用。本次运行跟踪也没有实时策略证据；这不是对所有可能版本、插件或其他部署配置的保证。

## 工程处理

保留原始离群点与原始性能报告，继续把每个 cgroup 的累计计账作为预算和总体负载指标；短窗口峰值必须注明采样间隔与内核计账边界。20% 配额能压低和摊开恢复工作，但不能宣传成所有硬件中断、驱动执行和所有 20 ms 窗口都不超标的承诺。此次调查没有因一个尚未归因的峰值增加常驻线程、额外扫盘或新的生产跟踪开销；诊断工具仅在实验 VM 中开启。
