# 完整 Guard 性能证据汇总

`performance_probe.py` 在完整 Ubuntu VM 内测量根盘和两个数据盘的 Guard。
`performance_report.py` 只读取明确指定的原始报告，输出适合版本库保存的精简 JSON；
它不运行虚拟机、不访问块设备、不安装服务。

```bash
python3 lab/performance_report.py \
  lab/work/具体基线实验/report.json \
  lab/work/具体优化实验/report.json \
  --output lab/results/2026-10-01-performance.json
```

默认要求输入报告 `passed=true`，验收检查和离线 fsck 全部通过，源码和基础镜像
未发生变化，四个测量阶段齐全。工具还从原始 CPU 计数复核总量、均值及聚合方式。
失败实验只能用明确的 `--allow-failed` 开关汇总，输出继续标记失败，不能当作成功证据。

结果保留：

- 原始报告的路径与 SHA256、实际打包 payload 哈希、运行前后 root/data 文件哈希、
  当前 checkout 源码和 initrd 哈希、内核、实现版本、预热时间和 CPU 限额。
- 正常、无关事件风暴、相关事件风暴、断联恢复四阶段的完整资源统计。
- 实际事件数量/速率、实际采样窗口范围、限流时间、20 ms / 100 ms 窗口统计。
- 两种聚合峰值窗口的原始起止时刻、实际时长和 CPU 计数差；只保留极值对应窗口，
  便于核验突发峰值，不用把全部原始采样塞进版本库。
- cgroup 内存和采样前后的进程 RSS/PSS、私有页、换页、锁定页等快照。
- 根据各 Guard 日志 `waiting → ready` 定位的急救窗口，和覆盖窗口的资源统计。
- 实际拔插间隔、原工作负载最长等待、持久数据校验和全部验收结果。
- 恢复期间是否停止控制串口 RPC、明确的静默时长和 host 单调时钟窗口；历史报告
  未提供 `recovery_rpc_quiet` 时按 `false` 保存。静默实验应独立标记，不与原方法重复混算。

不会复制数千条原始采样、完整系统日志、磁盘身份或整份 VM 配置。
同一批输入、同一工具版本生成相同纯 JSON；输出没有当前时间或随机值。
如附加图像，图像哈希也进入 JSON，图像还取决于绘图库和字体版本；报告保留绘图库版本。
可用 `--label '实验版本说明'` 明确标记历史阶段；`variant=current` 仅表示该次实验
采用当时的工作区实现，不能推断它就是之后继续修改的最新代码。

## 可选独立图像

安装可选 `matplotlib` 后可增加：

```bash
python3 lab/performance_report.py \
  lab/work/具体基线实验/report.json \
  lab/work/具体优化实验/report.json \
  --output lab/results/2026-10-01-performance.json \
  --plot lab/results/2026-10-01-recovery.png
```

可重复 `--plot-report 输入报告路径` 只选择代表性的图像行，完整 JSON 仍保留所有输入。
例如多次 A/B 重复加两档配额的 8 场实验，可以全部写入 JSON，仅将基线、优化版、
10% 配额、5% 配额各选一场放入 4 行图像。

JSON 汇总只依赖 Python 标准库。绘图依赖仅用于离线报告，不进入 Guard 包。
图像使用原始采样计算滚动 5 个窗口、约 100 ms 的 CPU 均值；阴影来自同一
guest 单调时钟上的最早 waiting 到最后 ready。各行共享纵轴，避免不同自动缩放
让高占用和低占用看起来一样。CPU 分母为一个逻辑核心，不是整机核心总数。

## 口径边界

总量只加 **root cgroup + data slice**。后者已包括两个数据 Guard，不能再加子服务。
`quota_percent` 分别限制 root 和整个 data slice，不能误称两者合起来只有这一份配额。
CPU 只包含这些 cgroup 及其子进程；工作负载、采样器、udev 和外部内核线程不在其中。

CPU 数据来自 `cpu.stat` 的内核记账，不等于应用指令实际执行的纯净时间。本次 Ubuntu
内核未启用 `CONFIG_IRQ_TIME_ACCOUNTING` / `CONFIG_PARAVIRT_TIME_ACCOUNTING`；独立调度
跟踪发现，实验 RPC 使用的 `ttyS0` 中断有时被记在当时运行的 LVM 子进程账上。因此
短窗口可能包含实验传输中断的记账影响；采样器本身虽在别的 cgroup，实验附带开销
仍不能保证完全排除。跟踪实验有额外开销，仅用于诊断，不能混入常规性能曲线。
具体证据与未解决的边界见 [CPU 尖峰调查](../research/2026-10-01/CPU-SPIKE-DIAGNOSTIC.md)。

各组计数器也按顺序读取，并非原子快照；主性能采样器使用读取前的行时间戳，
未记录每个计数器读取前后的时间。20 ms 极值对组内读取延迟和记账时点更敏感。
这些边界不能证明某个异常峰值的唯一原因，也不能据此删除异常值或宣称硬实时限额。

20 ms / 100 ms 峰值都是窗口平均，不是硬件瞬时峰值。固定 16 秒恢复场景含故障前后
健康时间；其中均值与日志限定的急救窗口均值分开保留。RSS/PSS 是前后快照，没有被
包装成连续监视得到的进程内存峰值。cgroup 内存包含记账页缓存，不能与 PSS 相加。
`memory.peak` 和进程 `VmHWM` 没有在每个阶段重置，是服务/进程生命周期高水位；
阶段内采样得到的最高值另以 `memory_sampled_peak_bytes` 表示。

## 本次保存的证据

- [最终标准实验 JSON](results/2026-10-01-performance.json)：3 次基线、3 次最终 20% 配额、
  最终 10% / 5% 配额各 1 次；最终版本包括事件筛选与紧凑异常位置记录。
- [最终恢复时间线](results/2026-10-01-recovery.png)：基线、最终 20% / 10% / 5% 配额各一行。
  20% 行选取该版本约 100 ms 峰值最高的一次；另一次实验的最坏 20 ms 峰值仍保存在 JSON。
- [事件筛选阶段 JSON](results/2026-10-01-event-filter-performance.json) 和
  [对应时间线](results/2026-10-01-event-filter-recovery.png)：保留紧凑异常记录加入前的 8 场实验，
  用于区分两次优化的作用，不作为最终版本数据。
- [停止恢复期控制 RPC 的独立对照](results/2026-10-01-quiet-recovery-performance.json)：
  基线和最终版本各 1 次、各 20% 配额；重连后控制 RPC 静默 17 秒。该对照检查观察方法
  对计数的影响，不并入标准实验的重复统计，也不能凭一个样本证明异常尖峰已被消除。

结果解释和统计表见 [整体验收报告](../research/2026-10-01/OVERNIGHT-OPTIMIZATION.md)。
