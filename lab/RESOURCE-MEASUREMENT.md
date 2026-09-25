# Guard 资源测量口径

`measure_guard.py` 在独立完整 Ubuntu VM 中测量 Guard，宿主不运行 Guard、不修改真实块设备。将下例的 `lab/work/current` 换成当前 Linux 7.0 的匹配构建目录：

```bash
python3 lab/measure_guard.py --build-dir lab/work/current
python3 -m unittest discover -s lab/tests -p 'test_measure_guard.py' -v
```

运行时只保留这一台测量 VM，避免把并行实验的宿主争用误解为项目开销。运行器将源代码哈希、构建记录、guest 串口原始输出与结构化结果写入 `lab/work/*-guard-budget/`。

## 本轮实测

2026-09-25，使用最终 `lab/work/route-refactor-v4` 构建、2 vCPU / 3072 MiB 的完整 Ubuntu VM。测量窗口内没有其他实验 VM。原始结果为 `lab/work/20260925-154008-guard-budget/report.json`，SHA256：`a1fae412f829e738c17ac04a59b85c58c68cf0b19c3a56087b02c5aa6d3a42d0`。统一公开摘要见 [重构实验结果](results/2026-09-25-route-refactor.json)。

| 阶段 | 时长 / 样本数 | 平均 CPU | P95 | P99 | 窗口峰值 |
|---|---|---|---|---|---|
| 健康空闲 | 30.106 秒 / 300 | 0.04549% | 0.43073% | 0.56725% | 0.66765% |
| 约 100 条事件/秒 | 10.057 秒 / 100 | 0.49579% | 0.79763% | 0.95915% | 1.16617% |

两阶段均保持 ready、零恢复、无探测事件、单线程、无观测子进程、无子进程 CPU tick 增长，cgroup 节流次数未增加。旧 CPU 验收与新增内存观测检查全部通过。这个样本符合既定健康 CPU 工程目标，不据此进行跨版本性能排名。

| 四个边界快照的内存视图 | 实测 |
|---|---|
| Guard 进程 PSS | 13.454–13.470 MiB |
| Guard 进程 RSS | 19.375–19.391 MiB |
| Guard cgroup `memory.current` | 8.965–9.230 MiB |
| cgroup 生命周期 `memory.peak` 的最大读数 | 9.230 MiB |
| `memory.max` / `memory.swap.max` | 128 MiB / 0 |
| Swap、SwapPss、VmSwap、VmLck、Locked | 均为 0 |
| `memory.events` 与 `memory.events.local` | 所有计数均为 0 |
| `/run/rescue` tmpfs | 使用 86.395 MiB；容量 256 MiB；noswap |
| `/run` tmpfs | 使用约 0.859–0.871 MiB；noswap |

所有字段读取成功，guest 没有启用 swap。这些数值不能相加：PSS 比该组 `memory.current` 大也不矛盾，进程可以映射已由其他 cgroup 记账的共享工具/库页。约 86 MiB 是整个救援工具 tmpfs 的分配量，不是 Guard 的私有堆；采样器和 agent 也使用这个环境。此健康观测没有包含失联时脏页/队列的峰值。

## CPU 与健康行为

原有测量流程和验收不变：30 秒健康空闲、10 秒每 100 ms 注入 10 条 DM change 事件；每约 100 ms 读取 Guard cgroup `cpu.stat`。100% 表示一个 guest vCPU。采样器和事件产生器在 Guard cgroup 之外，cgroup CPU 计数包含 Guard 及其子进程；报告平均值、P95、P99、观察窗口峰值和节流计数。

P99 可对照技术路线的“100 ms 窗口 P99 < 5%”工程目标，峰值仍单独保留。一次机器上 300/100 个样本的分位数不是跨硬件、长期负载或任意瞬间的硬上限；未把分位数统计替换成服务存活或内存压力保证。

继续要求 ready、零次恢复、同一进程实例、无 probing 事件、单线程、无观测子进程、子进程 CPU ticks 没有增长。小于采样间隔的短任务可能没有出现在进程列表中；CPU 计费仍包含在 cgroup 中。`4000 20000` 的 CPU 配额不是任意瞬间的峰值承诺，也不包含内核其他 worker 或原应用的开销。

## 内存观测

每个 CPU 阶段开始前与结束后各采集一次内存快照，**不在 100 ms CPU 窗口中反复读取 smaps**。这是健康期的四个边界快照，不是内存压力或瞬时峰值测试。

| 视图 | 结果字段 | 口径 |
|---|---|---|
| Guard 及同 cgroup 子进程 | `memory_*.guard_cgroup.processes` | 枚举该 cgroup 及后代 cgroup 的全部 PID，读取 status、smaps_rollup；保存 PSS/RSS/Swap/SwapPss/Locked/VmLck/VmSwap |
| cgroup 记账 | `memory_*.guard_cgroup.files` | `memory.current/peak/stat/events/events.local`、swap current/peak/max 和内存限额；缺失项为 null 并记录 errno |
| RAM 文件系统 | `memory_*.tmpfs.mounts` | `/run/rescue` 和 `/run` 的 mount 类型、noswap 选项、容量、实际分配量和可用量；记录设备号及重复挂载关系 |
| 整个 guest | `memory_*.guest_meminfo_bytes`、`guest_swaps` | 系统内存、缓存、脏页、swap 配置背景；不能直接归因给 Guard |

`memory.peak` 是该 cgroup 生命周期内的内核记账峰值，包含测量开始前的启动；未重置它，也不将它改名为“本阶段峰值”。内存读取不是原子快照；读取过程中消失或 PID 被复用的进程保留未知值，避免将缺失计成零。`process_totals_bytes` 只有全部被枚举进程的对应值均可读时才给出总数。

## 不能直接相加的数值

- RSS 包括共享映射，直接加进程 RSS 会重复计入共享页。PSS 按共享比例分摊，但只描述进程映射。
- `memory.current` 还包括计入该 cgroup 的页缓存和部分内核内存，既可能与 PSS 重合，也有 PSS 未覆盖的项目。`memory.stat` 中的 file/shmem 等字段也存在包含关系。
- tmpfs 的使用量包括被进程映射的工具和库，并且 `/run/rescue`、`/run` 还被救援 shell、agent 和采样器使用。它们不是 Guard 独占内存。容量上限也不是已使用量。

因此不计算 `PSS + memory.current + tmpfs used` 之和。本测量没有无保护的同负载基线，不能给出“整个保护系统增加了多少物理 RAM”的最终数字。为完整成本归因仍需独立基线、共享页归属及故障期间的队列/脏页增量实验。

## noswap 的边界

`tmpfs,noswap` 使对应文件系统中的文件页不进入 swap，**不等于锁定 Python 的匿名堆和栈**。本轮另给 Guard cgroup 设置 `memory.swap.max=0`、`memory.max=134217728`，Ubuntu unit 对应 `MemorySwapMax=0`、`MemoryMax=128M`，最小 guest 设置等价的 cgroup 参数。前者禁止该组及其后代的匿名页进入 swap，后者限制计入该组的内存；测量会记录并单独检查这两个实际值。

这仍不是 mlock，不能预期 VmLck/Locked 因而上升；达到上限时可能回收或 OOM。由其他 cgroup 预装并记账的 tmpfs 文件页，不会仅因 Guard 后来读取它们就全部计入 Guard 的 128 MiB 上限。因此上限不代表整个 RAM 救援环境最多占 128 MiB。Swap 为零且 guest 没启用 swap，也不能证明在另一台有 swap、内存紧张的机器上保持可调度。

Ubuntu 的 Guard 单位和停止后的接管命令使用 `RootDirectory=/run/rescue` 直接执行 RAM 中的 Python，避免把故障盘上的 chroot 可执行文件当作接管依赖；这不改变内存记账范围，也不替代内存压力与线程卡死实验。

目前这份测量不验证内存压力、OOM、长时间失联、最小监督者存活、全故障域的 RAM 上限，也不验证真实工作机的救援驻留策略。资源观测补齐不能替代这些阶段 D 验收。

统计字段依据：[内核 /proc 文档](https://www.kernel.org/doc/html/latest/filesystems/proc.html)、[cgroup v2 内存控制器](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)、[tmpfs 文档](https://www.kernel.org/doc/html/latest/filesystems/tmpfs.html)。运行中的字段和限制值以该次结果为准。
