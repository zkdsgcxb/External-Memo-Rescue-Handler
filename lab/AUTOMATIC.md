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

主机只发 QMP 拔插命令，**不提前通知 guest，也不预暂停 LV**。路径错误首先由内核排队层处理；后台轮询负责发现失败、核验身份和恢复路径，不能靠它的轮询速度抢在每次 EIO 前。物理盘层的内核 I/O error 日志仍可能出现，成功标准是错误没有扩散成被测文件系统和应用的失败。

Guard 当前每次执行 `step()` 后休眠 0.1 秒，检查旧设备 sysfs 节点是否存在、解析后的路径是否改变，以及 `dmsetup status` 是否标记路径失败。它没有订阅 udev/netlink，也没有使用 eBPF。100 毫秒是每轮结束后的休眠，不是严格的检测时限：命令执行和核验也占时间。内核预置的排队机制不等待这次轮询，因此检测延迟不等于错误可自由扩散的空窗。后续可采用设备事件唤醒加周期复核，事件仅触发核验，不直接授权接盘。

后台复用生产 `Recovery.verify()`，检查 USB VID/PID/序列号、容量、分区 UUID、PV/VG 身份，再检查分区长度及完整 LV 段布局。持有候选设备文件描述符并再次核验后，加载 inactive table，在故障发生后短暂执行 noflush/nolockfs suspend/resume 切换底层路径。上层 LV 不刷新、不重建，文件系统不卸载、不重挂载，也不执行 fsck。

默认后台等待窗口为 8 秒；内核无路径排队超时为该值加 2 秒，作为后台死亡时的补充退路。计时起点和调度不同，不是严格实时上限。超时后切成 `fail_if_no_path`，允许 I/O 报错，并保持终止状态，不会在盘晚归后偷偷重新启用写入。此时只能保留 RAM 救援、诊断与后续人工处理，不能保证原系统完整存活。

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
