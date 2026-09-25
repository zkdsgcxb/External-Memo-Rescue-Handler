# 新内核路径探测接入（仅虚拟机）

2026-09-25：Guard 已接入 Linux 6.16 起的 `DM_MPATH_PROBE_PATHS`。本机 7.0 提供该接口，代码和故障试验仍限于 QEMU；源码提交不部署真实根盘自动保护。

## 替换的逻辑与保留的职责

删除了 `resume` 后直接清空 deadline、增加 recoveries 并记录 ready 的分支，统一改为：

```text
身份与布局准入 → 最终实例复核 → load / suspend / resume
    → 新内核单次路径探测 → DM / sysfs / dev_t / 原期限复核 → ready
```

内核负责遍历当前活动路径组中的 Active 路径、各读取一个逻辑块，并对路径类错误执行 fail_path。Guard 负责决定何时调用、约束调用生命周期和解释结果；不实现第二份逐路径读取/标坏算法。此前 Guard 本来没有健康读盘探针，所以此次替换的是恢复完成分支，不能声称删除了一套原有读盘算法。

USB/容量/分区/PV/VG/LV 布局核验、最后一次实例复核、唯一映射管理者与恢复期限继续保留。内核探测发生在已准入映射 resume **之后**，不能作为新盘放行前的身份门禁，也不阻止已恢复路径上的业务 I/O。

## 实现和资源约束

- `guest/dm_monitor.py` 的 `DeviceMapper.target_version()` 在启动时通过已加载的 libdevmapper 查询内核 multipath target 版本，不启动外部命令；上游 1.15.0 起有该接口。未提升 target 版本的厂商回移可能保守漏用，不能仅凭内核版本字符串猜测。
- `PathProbe` 直接对 multipath 块设备 FD 发出 ioctl，不经 `/dev/mapper/control`。使用本实验 x86_64 ABI 的 `0xfd12`；没有编译新内核或添加 eBPF。
- 健康期零探测、零探测线程。仅换表后按需启动一个 daemon thread，仍在 Guard 的 cgroup 中；只允许一个未完成或未消费结果的任务，没有 worker 队列或备用并行探针。
- `guest/path_guard.py` 在 `probing` 状态等待结果。探测可能同步阻塞并持有内核 live-table 引用，期间不再 load/suspend，不重新扫描候选盘；线程无法保证取消下层内核 I/O。
- 使用原来的恢复 deadline，不因探测重置。resume 已跨过期限时不发新探测；到期后只关闭无路径排队并保持 expired，迟到完成不能恢复 ready。此动作不取消在途请求，也不是对仍活动路径的全局写入禁止。
- 结果带映射 generation；完成后检查同一 sysfs 实例、同一 dev_t 在 DM 中为 Active、UUID/target 正确和期限未过。返回 0 记录为 `completed`，没有 `read_verified` 或“整个系统健康”的含义。

旧 target 在调用前降级，记录 `status=unsupported, source=feature-check, errno=null`，随后仍检查路径状态。真正的 ENOTTY 记录并缓存为 `cached-unsupported`。**新内核返回 EINVAL/EIO 等其他错误时不降级、不标 ready。** 未支持平台不会偷偷运行另一套自写读探针。

内核可能在 current_pg 尚未选中、初始化或暂停准备期间跳过读取；也不会凭一个逻辑块证明文件系统/数据健康。接口不会重新启用 Failed 路径。恢复后的直接块读取、实际写入/fsync、文件前缀和 Git 验证仍由独立验收完成；`auto_run.py` 已加强块读取判据，要求真实返回 4096 字节及正确 ext4 magic。

## 直接 ABI 实验

由 `kernel_probe_test.py` 给可丢弃 VM 添加独立空白 USB 虚拟盘，创建 `linear → multipath` 测试映射；不改变宿主块设备，也不改变 guest 根盘映射。读取次数来自下层 DM 的内核统计。

| 场景 | 实测 |
|---|---|
| 7.0，current_pg 尚未选中 | ioctl 返回 0，但下层读取数仍 0；不能把 0 当读成功 |
| 7.0，已选中的 Active 路径 | ioctl 返回 0，下层读取数 1 → 2 |
| 下层映射 suspend | 调用持续阻塞超过 2 秒，RAM shell 存活；resume 后原调用完成 |
| 下层切为 error target | 原 A 路径变 F，返回 ENOTCONN |
| 修复下层后再次探测 | 仍为 F/ENOTCONN、没有新增读取；接口不负责 reinstate |
| 6.8，旧 target | 返回 EINVAL，路径仍 A、没有探测读取；直接 USB multipath 和普通块设备同样返回 EINVAL |

最终两轮共 17 项检查通过。首次调试中遗漏 BIO 配置导致建表失败，以及错误假设旧版必为 ENOTTY 的失败记录均保留，不混为成功实验。[结果和哈希](results/2026-09-25-kernel-probe-api.json)

## Guard 集成与验收

单元测试覆盖异步阻塞、单任务上限、错误及能力降级、generation/实例变化、健康期不读盘、过期后迟到结果不可复活，以及 resume 自身跨过期限。它们不会访问真实块设备。

最终源码的实验室 27 项与原救援工具 14 项测试通过；其中 22 项为此次新增。最终构建的集成观测如下，全部运行均核对构建源码和 runner 哈希：

| 场景 | 结果 |
|---|---|
| 7.0，完整 Ubuntu / LVM 根卷，Git 克隆中同端口连续三次 `gap=0` 拔插 | 三次均走内核探测确认；213 次成功写入、0 错误；原系统/进程/服务存活；直接块读取、前缀审计和 Git 完整性检查通过 |
| 6.8，最小根系统同端口三次重接 | 三次均按 target 1.14.0 走 `feature-check` 降级；81 次成功写入、0 错误；没有发出不支持的 ioctl |
| 7.0，同序列号/分区标识但不同 PV 的盘重接 | 拒绝准入，无探测、无成功恢复；约 4 秒窗口结束后进入 expired，RAM 通道保持可用 |

7.0 完整系统样本最长应用写入约 2.086 秒，RAM 心跳最大间隔约 0.665 秒。`gap=0` 表示 QMP 删除确认后无额外等待，不等于物理断联或业务停顿为零。这几组故障测试并行运行，不用于跨版本性能排名；健康 CPU 测量另行单独运行。

随后单独测量完整 Ubuntu 中的 Guard cgroup，包含其子进程预算，采样器位于该 cgroup 外：

| 阶段 | 平均 CPU（一个 guest vCPU = 100%） | 约 100 ms 窗口峰值 |
|---|---:|---:|
| 空闲约 30.35 秒 | 0.1223% | 1.5551% |
| 每 100 ms 发送 10 个块设备事件，约 10.24 秒 | 0.8692% | 2.3361% |

400 次采样均为一个线程、无观测子进程；累计 probing=0、recoveries=0，子进程 CPU tick 无增长，两阶段都没有增加 cgroup 节流次数。RSS 为 14.72 MiB，含共享页，不等于独占内存或整个救援环境用量。采样峰值不是任意时刻的硬上限，也不能排除短于采样间隔的任务；本轮不据此宣称相对旧版性能提升。

完整集成与健康开销的最终结果见 [集成摘要](results/2026-09-25-kernel-probe-integration.json)。这些结果不证明真实 USB 电气故障、所有文件系统/应用、断电耐久性或任意管理器死亡时刻都可恢复。

## 复现

先按 [实验室说明](README.md) 构建匹配内核的 guest；以下目录是本次保留的最终构建。完整 Ubuntu/Git 还需要原有种子镜像。

```bash
python3 lab/kernel_probe_test.py --build-dir lab/work/kernel-probe/guest7-final --expect available
python3 lab/kernel_probe_test.py --build-dir lab/work/kernel-probe/guest68-final --expect unsupported
python3 lab/auto_run.py --build-dir lab/work/kernel-probe/guest7-final --guest ubuntu --workload git-clone --cycles 3 --same-port --gap 0
python3 lab/auto_run.py --build-dir lab/work/kernel-probe/guest68-final --cycles 3 --same-port --gap 0
python3 lab/auto_run.py --build-dir lab/work/kernel-probe/guest7-final --reconnect wrong --queue-seconds 4
python3 lab/measure_guard.py --build-dir lab/work/kernel-probe/guest7-final
python3 -m unittest discover -s lab/tests -p 'test_*.py' -v
```

CPU 测量单独运行，不与其他 VM 实验并行。原始日志和镜像留在 Git 忽略的 `lab/work/`，公开摘要保留来源、哈希和验收范围。

来源：[v7.0 探测实现](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-mpath.c#L1912)、[ioctl 的 live-table 生命周期](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm.c#L392)、[6.8 的旧 ioctl 转发](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm.c#L429)、[接口引入及 target 版本提升](https://github.com/torvalds/linux/commit/7734fb4ad98c3fdaf0fde82978ef8638195a5285)、[libdevmapper ABI](https://github.com/lvmteam/lvm2/blob/v2_03_16/libdm/libdevmapper.h)。
