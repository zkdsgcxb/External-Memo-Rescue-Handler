# 后台自动恢复原型（仅虚拟机）

2026-09-24 新增 [完整 Ubuntu Server 与 Git 克隆实验](UBUNTU.md)。本文以下数字记录的是 2026-09-23 的最小 guest，完整系统结果单独记录，不混用验收范围。

本机已安装版仍是 F9/F10 手动 RAM 救援终端。这里的新原型在启动时就建立排队层，突发掉盘后由内核暂存可重试的路径 I/O，RAM 中的后台程序核验重接设备并切换路径，不需要用户敲命令，也不依赖桌面弹窗。故障期间访问根盘的程序可能等待；恢复后继续，并非零延迟。

## 放置位置与机制

```text
原工作进程 → ext4 → LVM LV → lab-path（稳定 DM multipath）→ USB 分区
                                   ↑
                    RAM 后台核验、路径切换、超时管理
```

`lab-path` 位于 PV 下方，上层 LV 一直引用同一个 DM 设备。实验使用 Linux 6.8 已有的 dm-multipath BIO 模式和 `queue_if_no_path`，没有 eBPF、自编内核模块或 multipathd。请求模式不支持本实验的分区后端；BIO 模式已实测。

主机只发 QMP 拔插命令，**不提前通知 guest，也不预暂停 LV**。路径错误首先由内核排队层处理；后台监视负责发现失败、核验身份和恢复路径，不能靠它的响应速度抢在每次 EIO 前。物理盘层的内核 I/O error 日志仍可能出现，成功标准是错误没有扩散成被测文件系统和应用的失败。

Guard 通过内核 kobject netlink 接收块设备及 DM 的 uevent，阻塞等待事件，并在每次健康检查完成后 1 秒兜底复核。状态查询使用常驻加载的 `libdevmapper`（Python ctypes），不再为健康轮询启动 `dmsetup` 子进程。故障判定仍是旧 sysfs 节点消失、解析路径改变或 DM 路径标记失败；没有新增 I/O 堵塞推断、健康期读盘探针或 eBPF。事件只是唤醒线索，不能授权接盘；当前监听全部块设备事件，最终只查询登记映射，只有异常后才寻找登记 USB 身份。

单线程状态机确保最多一个恢复流程。每次最多接收 64 条事件，健康复核最多每 100 ms 一次，重复事件合并；无关事件也有处理节流，丢包后由状态复核兜底。恢复重试按 100、200、400、800 ms 退避并封顶，事件不能绕过重试时间。先经 sysfs 确认唯一登记磁盘、容量、分区和设备节点就绪，再调用 blkid/LVM。完整校验和切换前的再次核验保留；不新增并行扫描任务。

Ubuntu 中 `lab-guard.service` 配置 `CPUQuota=20%`、`CPUQuotaPeriodSec=20ms`；最小 guest 创建独立 cgroup v2 并设置 `cpu.max=4000 20000`。子进程继承同一 cgroup，不能逃离配额。配额控制调度周期内的总 CPU 用量，不是任意瞬时窗口的严格峰值承诺，也不覆盖内核 DM 工作线程或恢复后应用的运行开销。内核排队不依赖 Guard 的调度及时性。

后台复用生产 `Recovery.verify()`，检查 USB VID/PID/序列号、容量、分区 UUID、PV/VG 身份，再检查分区长度及完整 LV 段布局。持有候选设备文件描述符并再次核验后，加载 inactive table，在故障发生后短暂执行 noflush/nolockfs suspend/resume 切换底层路径。上层 LV 不刷新、不重建，文件系统不卸载、不重挂载，也不执行 fsck。

2026-09-25 已用 **`probing → 路径复核 → ready` 替换原来的 resume 后直接 ready**。启动时通过 libdevmapper 查询实际 multipath target 版本；1.15.0 起使用 `DM_MPATH_PROBE_PATHS`，由内核读取当前活动组的活动路径并标记路径类错误。仅在已准入的新表 resume 后按需启动一个后台线程，健康期不启动探测线程或读取。完成后复核映射 UUID/类型、当前 dev_t 的 Active 状态、sysfs 实例和原恢复期限，再记录 ready；返回 0 只记作 `completed`，不解释为读成功或文件系统健康。

未完成探测持有内核 live-table 引用，期间禁止再次 load/suspend；超过原期限则保持 expired，迟到结果不能复活恢复流程。后台线程不能取消卡在内核的读取。旧 target 不发新 ioctl，明确记录 `state-only-unsupported`；真正的 ENOTTY 会缓存，不重复调用。旧 6.8 实测可能将未知 ioctl 转发并返回 EINVAL，所以新内核的 EINVAL 必须按错误处理，不能统一降级。实现、单独 ABI 实验和集成结果见 [新内核路径探测接入](KERNEL-PROBE.md)。

默认后台等待窗口为 8 秒，包含切换后的探测确认阶段；内核无路径排队超时为该值加 2 秒，作为后台死亡时的补充退路。计时起点和调度不同，不是严格实时上限。超时后切成 `fail_if_no_path`，结束无路径排队，并保持终止状态，不再自动装入新表。它不取消下层在途 I/O，也不禁止仍为 Active 的路径继续工作；此时不能保证原系统完整存活。

## 复现

先按 [README](README.md) 构建；无需 root。每次使用全新虚拟盘，不接触宿主块设备。

```bash
python3 lab/auto_run.py --cycles 3 --gap 1
python3 lab/auto_run.py --transport bot --cycles 3 --gap 0.2
python3 lab/auto_run.py --reconnect none --queue-seconds 4
python3 lab/auto_run.py --reconnect wrong --queue-seconds 4
python3 lab/auto_run.py --reconnect late --queue-seconds 4 --gap 6
python3 lab/auto_run.py --reconnect none --queue-seconds 4 --kill-manager
```

`wrong` 预制同容量、同分区 UUID 但不同 PV UUID 的盘，再用原 USB 序列号和协议接入，验证不能只靠序列号放行。`late` 在等待窗口后接回正确盘，要求保持失败终态。`--kill-manager` 在断联期间杀死 RAM 路径管理程序，检查内核超时退路。故障组中的应用 EIO 是预期结果，不代表恢复成功。

报告在 `lab/work/*-auto-*/report.json`，`passed` 表示该场景的验收通过。摘要和源文件/镜像哈希见 [自动实验结果](results/2026-09-23-automatic.json)。原始报告留在本机，未上传磁盘镜像。

## 实测与边界

2026-09-23，Ubuntu 6.8.0-139-generic / QEMU 8.2.2 / KVM：

| 场景 | 应用成功/失败写入 | 最长写入等待 | 结果 |
|---|---:|---:|---|
| UAS，1 秒间隔，连续 3 次断联 | 65 / 0 | 1.88 秒 | 自动恢复，原进程继续 |
| BOT，0.2 秒间隔，连续 3 次断联 | 67 / 0 | 2.12 秒 | 自动恢复，原进程继续 |
| 同序列号、同分区 UUID、错误 PV | 13 / 25 | 4.20 秒 | 拒绝接入，等待超时 |
| 正确盘在 6 秒后晚归，窗口 4 秒 | 9 / 43 | 4.26 秒 | 保持失败终态 |
| 管理程序被杀，窗口 4 秒、内核超时 6 秒 | 12 / 34 | 6.49 秒 | 内核结束排队，救援可用 |

两组重复恢复均验证同一工作进程 PID/启动时间、上层 DM 依赖不变、根文件可读、直接块读取成功、实际写入与 fsync 成功、journal 未中止，并回读所有已确认写入的文件前缀。以上五组 RAM 心跳最大间隔小于 0.81 秒，独立 shell 在断联期间及之后都可用。另行验证无重连时的后台超时，以及原手动方案的基线/预暂停回归和 13 项恢复单元测试。

这些结果只证明最小 guest 中一个简单应用的恢复。尚未验证完整 Ubuntu/systemd/桌面/数据库、真实 Hub 故障、供电丢失的数据持久性、长期反复掉线、磁盘内部缓存及部分完成的写入。文件回读也不是断电一致性证明。UUID 和序列号不是防恶意克隆的密码学身份。

后台命令阻塞、内核排队超时与路径切换同时发生、切换期间后台死亡等竞态仍需专项验证；内核兜底已测的是“无路径等待时后台死亡”，不能扩大为任意死锁都能恢复。此原型不包含真实根盘启动迁移或安装器，不可把 guest 的建盘命令用于宿主磁盘。

参考：[Linux 6.8 dm-multipath 实现](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c)、[Linux DM multipath 表示例](https://docs.kernel.org/admin-guide/device-mapper/dm-queue-length.html)。
