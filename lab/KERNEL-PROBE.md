# 当前内核路径探测与共享 Guard

当前 Guard 已接入 `DM_MPATH_PROBE_PATHS`，只维护 **Linux 7.0.0-34-generic / x86_64** 基线，已删除旧内核兼容与降级分支。核心统一位于 [`guard/runtime/`](../guard/runtime/)，`lab/build.py` 直接复制同一份实现进入 guest 的 `/opt/lab`，构建 manifest 记录共享源码路径与哈希。

可选实机 initramfs 启动接入已实现，采用同一核心；**可选入口已安装，实机尚未重启、完成启动验收，当前会话没有自动保护**。实机入口要求显式 `ram_rescue_guard=1` 与登记内核 release 完全匹配，先核验并激活已有 LV，切根后由 systemd 取得运行期间的 owner。安装与启动边界见 [`guard/README.md`](../guard/README.md)。本文下面的性能和故障样本均来自 QEMU，并分别标明历史版本。

## 替换的逻辑与保留的职责

删除了 `resume` 后直接清空 deadline、增加 recoveries 并记录 ready 的分支，统一改为：

```text
身份与布局准入 → load inactive → 再次核验布局与实例
    → 单次 resume --noflush --nolockfs → 探测前实例复核
    → 内核路径探测 → DM / sysfs / dev_t / diskseq / 原期限复核 → ready
```

已删除独立的 `dmsetup suspend` 步骤。一次 resume 由内核完成必要的 suspend、swap、resume，缩小用户态停在显式暂停状态的窗口；它不是任何错误下均可自动回滚的事务。

内核负责遍历当前活动路径组中的 Active 路径、各读取一个逻辑块，并对路径类错误执行 fail_path。Guard 负责决定何时调用、约束调用生命周期和解释结果；不实现第二份逐路径读取/标坏算法。此前 Guard 本来没有健康读盘探针，所以此次替换的是恢复完成分支，不能声称删除了一套原有读盘算法。

USB/容量/分区/PV/VG/LV 布局核验、最后一次实例复核、唯一映射管理者与恢复期限继续保留。`admission.py` 持有候选只读 fd，将 diskseq、大小、布局、boot ID、owner epoch 和期限写入凭证；fd 不能直接指定 DM 最终绑定的内核设备对象。新实例复用当前 active 后端 dev_t 时，当前实现保守拒绝自动切换。内核探测发生在已准入映射 resume **之后**，不能作为新盘放行前的身份门禁，也不阻止已恢复路径上的业务 I/O。

## 实现和资源约束

- [`guard/runtime/dm_monitor.py`](../guard/runtime/dm_monitor.py) 的 `DeviceMapper.target_version()` 在启动时通过已加载的 libdevmapper 查询内核 multipath target 版本，不启动外部命令；要求至少 `1.15.0`，否则直接拒绝启动。它是依赖检查，不是版本适配器。
- 同一模块的同步函数 `probe_paths()` 直接对 multipath 块设备 FD 发出 ioctl，不经 `/dev/mapper/control`。使用 x86_64 ABI 的 `0xfd12`；没有新增自定义内核代码或 eBPF。原来独立管理线程的 `PathProbe` 已删除。
- [`OwnedOperation`](../guard/runtime/owned_operation.py) 统一执行可能阻塞的身份读取、最终核验、DM 变更和原生探测。健康期零探测、零 worker；故障期同一 owner 最多一个未完成或未消费结果的任务，没有 worker 队列或备用并行探针。完成通过 eventfd 唤醒主循环，不为每个成功阶段额外等待 100–800 ms 重试退避。
- [`path_guard.py`](../guard/runtime/path_guard.py) 主循环串行记录 journal、核对结果并推进阶段，worker 不修改 Guard 状态或 journal。`probing` 期间不启动第二次核验或换表。探测可能同步阻塞并持有内核 live-table 引用，线程不能强制取消下层内核 I/O。
- worker 与 DM、blkid、LVM helper 继承同一 flock 的 fd。完成但尚未消费的结果继续持锁；`poll()` 接收结果或废弃清理完成后才释放相应引用。主 owner 退出后，接管者必须等旧操作的锁引用消失，不能并行修改映射。
- 使用原恢复 deadline，不因任何阶段重置。主循环到期先写 `expired`、停止准入并废弃迟到结果；排队关闭记录为 `deferred_to_takeover`，由拿到锁的接管者协调已登记表后发送 `fail_if_no_path`，完成才记录 `completed_by_takeover`。内核无路径计时独立存在；准入到期不等于排队已关闭，也不等于所有在途 I/O 已完成或已取消。
- 每个操作结果带 kind/token，探测结果另外带映射 generation；完成后检查同一 sysfs/diskseq 实例、同一 dev_t 在 DM 中为 Active、UUID/target、表摘要和原期限。返回 0 只记录为 `completed`，没有 `read_verified` 或“整个系统健康”的含义。迟到结果只能清理资源，不能使 expired 重新 ready。

接管只清理可信 journal 已登记的事务并终止准入：可清除已知 inactive，必要时恢复已知 active 的暂停，再关闭无路径排队。它不重新准入旧候选，不把尚未提交的 inactive 自动晋升；未知表进入 blocked。flock 只协调本项目控制器，无法排斥其他 root 程序擅改映射。完整合同见 [恢复事务](TRANSACTIONS.md)。

探测只有 `completed`、`no_paths`、`error` 三种结果。**ENOTTY、EINVAL、EIO 等错误均不会降级或标 ready**；已删除能力缓存、伪造的未执行探测结果以及 `state-only-unsupported` 完成分支。恢复就绪必须经过实际 ioctl 完成和路径复核。

内核可能在 current_pg 尚未选中、初始化或暂停准备期间跳过读取；也不会凭一个逻辑块证明文件系统/数据健康。接口不会重新启用 Failed 路径。恢复后的直接块读取、实际写入/fsync、文件前缀和 Git 验证仍由独立验收完成；`auto_run.py` 已加强块读取判据，要求真实返回 4096 字节及正确 ext4 magic。

## 7.0 接入时的基线复验（历史记录）

2026-09-25 删除兼容分支时，25 项实验室单元测试和 7.0 的 13 项直接 ABI 检查通过。完整 Ubuntu/LVM 根卷在 Git 克隆中同端口连续三次 `gap=0` 拔插，三次均实际调用内核接口后完成路径复核；233 次成功写入、0 错误，原进程/服务、直接块读取、数据前缀审计及 Git 完整性检查通过。最长观测写入约 1.043 秒，非保证上限。

该次构建、运行源码与报告哈希见 [7.0 接入基线摘要](results/2026-09-25-kernel7-only.json)。它记录共享核心及 OwnedOperation 重构之前的源码，不能作为当前工作树重新运行的证据；旧内核记录不作为当前支持范围。

## 直接 ABI 实验

以下保留接口接入初版 `88b8e55` 的实验记录；旧版对照仅作为历史证据，当前 runner 已移除旧版模式。

由 `kernel_probe_test.py` 给可丢弃 VM 添加独立空白 USB 虚拟盘，创建 `linear → multipath` 测试映射；不改变宿主块设备，也不改变 guest 根盘映射。读取次数来自下层 DM 的内核统计。

| 场景 | 实测 |
|---|---|
| 7.0，current_pg 尚未选中 | ioctl 返回 0，但下层读取数仍 0；不能把 0 当读成功 |
| 7.0，已选中的 Active 路径 | ioctl 返回 0，下层读取数 1 → 2 |
| 下层映射 suspend | 调用持续阻塞超过 2 秒，RAM shell 存活；resume 后原调用完成 |
| 下层切为 error target | 原 A 路径变 F，返回 ENOTCONN |
| 修复下层后再次探测 | 仍为 F/ENOTCONN、没有新增读取；接口不负责 reinstate |

最终两轮共 17 项检查通过。首次调试中遗漏 BIO 配置导致建表失败，以及错误假设旧版必为 ENOTTY 的失败记录均保留，不混为成功实验。[结果和哈希](results/2026-09-25-kernel-probe-api.json)

## 当前实现的测试与历史集成样本

当前单元测试覆盖 OwnedOperation 的单任务上限、flock 引用继承、完成通知、迟到结果清理，以及恢复各阶段阻塞时先进入终态、kind/token/实例变化、健康期不读盘和截止后不可复活。[`test_existing_root_boot.py`](tests/test_existing_root_boot.py) 与 [`test_host_profile.py`](tests/test_host_profile.py) 另外验证 host 内核/启动参数门禁、已有 VG 拒绝、LV 依赖、状态目录隔离与 `READY=1` 通知；这些使用临时目录和模拟 DM/LVM，不替代实机启动测试。当前恢复重构与 VM 证据见 [重构记录](../research/2026-09-25/REFACTOR-RESULTS.md)。

接入初版 `88b8e55` 的实验室 27 项与原救援工具 14 项测试通过。该版本的集成观测如下，全部运行均核对构建源码和 runner 哈希；不把旧记录当作当前源码重跑：

| 场景 | 结果 |
|---|---|
| 7.0，完整 Ubuntu / LVM 根卷，Git 克隆中同端口连续三次 `gap=0` 拔插 | 三次均走内核探测确认；213 次成功写入、0 错误；原系统/进程/服务存活；直接块读取、前缀审计和 Git 完整性检查通过 |
| 7.0，同序列号/分区标识但不同 PV 的盘重接 | 拒绝准入，无探测、无成功恢复；约 4 秒窗口结束后进入 expired，RAM 通道保持可用 |

7.0 完整系统样本最长应用写入约 2.086 秒，RAM 心跳最大间隔约 0.665 秒。`gap=0` 表示 QMP 删除确认后无额外等待，不等于物理断联或业务停顿为零。这几组故障测试并行运行，不用于跨版本性能排名；健康 CPU 测量另行单独运行。

随后单独测量完整 Ubuntu 中的 Guard cgroup，包含其子进程预算，采样器位于该 cgroup 外：

| 阶段 | 平均 CPU（一个 guest vCPU = 100%） | 约 100 ms 窗口峰值 |
|---|---:|---:|
| 空闲约 30.35 秒 | 0.1223% | 1.5551% |
| 每 100 ms 发送 10 个块设备事件，约 10.24 秒 | 0.8692% | 2.3361% |

400 次采样均为一个线程、无观测子进程；累计 probing=0、recoveries=0，子进程 CPU tick 无增长，两阶段都没有增加 cgroup 节流次数。RSS 为 14.72 MiB，含共享页，不等于独占内存或整个救援环境用量。采样峰值不是任意时刻的硬上限，也不能排除短于采样间隔的任务；本轮不据此宣称相对旧版性能提升。

初版完整集成与健康开销见 [历史集成摘要](results/2026-09-25-kernel-probe-integration.json)，其中旧版兼容数据不代表当前支持范围。移除兼容分支后没有重新做性能排名。这些结果不证明真实 USB 电气故障、所有文件系统/应用、断电耐久性或任意管理器死亡时刻都可恢复。

## 复现

先按 [实验室说明](README.md) 从当前工作树构建匹配内核的 guest；以下 `GUARD_BUILD` 是新构建目录的示例，需替换为实际目录。不要使用上述历史镜像来代表当前共享核心。完整 Ubuntu/Git 还需要原有种子镜像。

```bash
GUARD_BUILD=lab/work/current-guard
python3 lab/kernel_probe_test.py --build-dir "$GUARD_BUILD"
python3 lab/auto_run.py --build-dir "$GUARD_BUILD" --guest ubuntu --workload git-clone --cycles 3 --same-port --gap 0
python3 lab/auto_run.py --build-dir "$GUARD_BUILD" --reconnect wrong --queue-seconds 4
python3 lab/measure_guard.py --build-dir "$GUARD_BUILD"
python3 -m unittest discover -s lab/tests -p 'test_*.py' -v
```

CPU 测量单独运行，不与其他 VM 实验并行。原始日志和镜像留在 Git 忽略的 `lab/work/`，公开摘要保留来源、哈希和验收范围。

来源：[v7.0 探测实现](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-mpath.c#L1912)、[ioctl 的 live-table 生命周期](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm.c#L392)、[接口引入及 target 版本提升](https://github.com/torvalds/linux/commit/7734fb4ad98c3fdaf0fde82978ef8638195a5285)、[libdevmapper ABI](https://github.com/lvmteam/lvm2/blob/v2_03_16/libdm/libdevmapper.h)。[6.8 的旧 ioctl 转发](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm.c#L429)仅用于理解历史对照，不是当前支持范围。
