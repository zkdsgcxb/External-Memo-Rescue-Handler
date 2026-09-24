# 完整 Ubuntu Server 实验

在原最小 guest 之外增加 Ubuntu Server 24.04 LTS 用户空间，PID 1 使用真正的 systemd，运行 journald、D-Bus 和由 systemd 管理的写入服务。根目录仍位于虚拟 USB → DM multipath → LVM → ext4 路径上。不是在健康 Ubuntu 中额外挂载一块故障数据盘。

这轮选择 Server Cloud rootfs，不含 GNOME 桌面。沿用匹配救援模块的 Ubuntu 6.8 内核和实验 initramfs，直接启动 `/sbin/init`，不经过安装器或 GRUB；因此覆盖完整 Server 用户空间，但不覆盖标准安装/引导器、桌面会话或所有应用。

## 构建与运行

```bash
# 在项目根目录运行，均不需要宿主 root 权限
python3 lab/fetch_ubuntu.py
python3 lab/build.py --kernel lab/work/kernel-package/boot/vmlinuz-6.8.0-139-generic

# Ubuntu 基线：不拔盘
python3 lab/auto_run.py --guest ubuntu --cycles 0
# UAS 突发断联与重复恢复
python3 lab/auto_run.py --guest ubuntu --cycles 3 --gap 1
# BOT 对照
python3 lab/auto_run.py --guest ubuntu --transport bot --cycles 3
# 无盘返回的超时退路
python3 lab/auto_run.py --guest ubuntu --reconnect none --queue-seconds 4
```

内核路径按本机实际匹配版本填写；依赖准备见 [README](README.md)。`auto_run.py` 默认仍为最小 guest，显式 `--guest ubuntu` 切换完整 Server。`--cycles 0` 是无故障基线；其余自动故障参数继续适用。

下载器固定官方镜像日期（默认 `20260911`），先通过 Ubuntu cloud image keyring 验证 SHA256SUMS 的 GPG 签名，再核对根文件系统归档 SHA-256。缺少 `/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg` 时停止，不跳过签名。镜像来源为 [Ubuntu 官方 Noble 构建目录](https://cloud-images.ubuntu.com/noble/20260911/)。

归档、只读初始化盘及来源哈希保存在 `lab/work/ubuntu/`；原始归档保留不变，另建按扇区对齐的副本。QEMU 将其作为只读 virtio 设备传入；guest 将归档解包到新建 USB 根卷，运行时系统不从初始化盘提供文件。每次新建 8 GiB 稀疏 USB 盘（根 LV 6 GiB）、3 GiB RAM、2 vCPU；无网卡、无共享目录或宿主块设备透传。保留的实验盘会逐渐占用项目磁盘空间。

## 集成方式

- 现有 initramfs 创建稳定 DM 层及 LVM/ext4，使用 GNU tar/xz 解包官方系统并保留权限、ACL 和扩展属性。
- 挂载并填充独立 RAM 工具环境后切换到 Ubuntu 的 `/sbin/init`。
- systemd 启动 `lab-agent`、`lab-guard`、`lab-shell`，这些服务在 RAM 工具目录中运行；失败不自动重启，避免把进程重启误报成存活。
- 写入进程由 Ubuntu 的 `lab-workload.service` 启动，从真实根盘执行 Python 并写入/同步根盘文件，不由 RAM agent 直接充当普通应用。
- 每次观察 Ubuntu 版本、已安装包数量、PID 1、boot ID、multi-user.target、journald/D-Bus/实验服务的状态、PID/启动时间和重启计数，以及 failed units。
- 保留原有直接块读取、根文件读取、实际写入/fsync、应用错误计数、确认数据回读、RAM 心跳和独立 shell 检查。
- 大型串口 JSON 报告使用 zlib/base64 无损压缩传输，主机解码后仍记录完整 JSON；心跳保持小消息，避免长时间 Git 负载累积的报告挤占心跳通道。仍保留原来的主机接收心跳间隔小于 2 秒的验收要求。

由于实验无网络或云元数据，禁用 cloud-init 和 network-online 等待；禁用 multipathd/LVM 自动扫描监控相关服务，避免它们与实验 Guard 同时管理同一映射；保留 LVM 工具和内核卷映射。ttyS1/ttyS2 的普通 serial-getty 被屏蔽，由实验通道占用。以上调整在新建 guest 内进行，不改宿主服务。

## 真实 Git 克隆中途拔插

```bash
# 首次准备真实 Linux v6.8 仓库与独立只读 Git 源盘
python3 lab/prepare_git.py
# 克隆基线和中途拔插；持续 fsync 负载也保留，分别验收
python3 lab/auto_run.py --guest ubuntu --workload git-clone --cycles 0
python3 lab/auto_run.py --guest ubuntu --workload git-clone --gap 0.2
```

源仓库从 kernel.org 的 Torvalds Linux 仓库下载，固定 v6.8、depth 1，包含该版本完整源码树而非完整历史。也接受已经从 kernel.org 官方镜像克隆的裸仓库：`prepare_git.py --source-repo <仓库目录>`。源提交、对象大小、文件数及独立源盘 SHA-256 随报告保存。

源盘使用 `mkfs.ext4 -d` 在普通文件内构造，通过 `debugfs` 设置镜像内仓库目录的 guest 属主；不要求宿主 root，也不修改宿主源仓库的所有权。这样保留 Git 的目录所有权检查，避免跨机器 UID 不一致导致克隆在故障注入前就失败。

guest 中独立只读 virtio 盘挂载在 `/srv/git`，真实 `git daemon` 只监听回环地址，Ubuntu 的 Git 客户端执行 `git clone --progress --depth 1 --branch v6.8 git://127.0.0.1/linux.git /root/linux`。目标和 Git 可执行文件都在故障 USB 根盘；源数据在独立盘。实验没有外网访问，也没有把仓库内容复制到目标来冒充 Git 克隆。

测试等待 Git 已经进入对象接收或工作树检出阶段，确认 Git 进程仍在，再直接 QMP 拔盘；断联期间再次观察同一进程。恢复后必须满足退出码 0、同一 Git PID/启动时间、HEAD 等于源提交、`git fsck --full` 成功、工作树干净。基线和故障组同样验收；如果 Git 在注入前就结束则报告失败，不算中途故障测试成功。

该场景覆盖真实 Git 协议、对象接收及检出过程，不覆盖公网网络抖动、TLS、凭证或完整 Linux 历史下载。当前版本未对 Git 故障注入阶段做固定速率控制，实际阶段和等待时间以报告为准。

重接由独立计时线程发出，避免串口观测拉长设定间隔。报告分别记录删除确认、重接请求和重接完成时间；这仍不是物理 USB 电气时序。`absent`、`git_while_absent`、`shell_while_absent` 是沿用的故障观测字段名，快速重接时观测可能跨过重接时刻；`shell_completed_before_reattach` 明确记录 shell 探针是否在重接请求前完成，不用字段名称推断时序。

## 结果解释

正向验收要求同一启动、PID 1 仍为 systemd、所有观测服务仍为同一实例且 active、没有 failed units，并满足原有恢复验收。负向实验要求有界结束等待且 RAM 救援可用，允许应用 EIO；不能用“负向实验通过”宣称系统已恢复。

后台存活不等于桌面、数据库、网络会话完整存活；当前覆盖受 systemd 管理的追加写入/fsync 和真实 Git 克隆两类工作负载。完整用户空间扩展了集成覆盖，不能替代真实硬件和应用级一致性测试。

## Ubuntu 集成验收记录（2026-09-24）

官方根文件系统报告 Ubuntu 24.04.5 LTS，603 个已安装软件包；实验使用 Ubuntu 6.8.0-139-generic 内核。以下为完成调试后的验收组：

| 场景 | 追加写入成功/失败 | 最长一次写入 | 结果 |
|---|---:|---:|---|
| Ubuntu 无故障基线 | 36 / 0 | 0.016 s | 通过 |
| Ubuntu UAS，1 s 间隔，连续 3 次断联 | 80 / 0 | 2.44 s | 通过 |
| Ubuntu BOT，0.2 s 间隔，连续 3 次断联 | 91 / 0 | 3.17 s | 通过 |
| Ubuntu 无重连，后台窗口 4 s | 12 / 26 | 4.99 s | 预期超时，RAM 救援可用 |
| 最小 guest 自动恢复回归 | 47 / 0 | 1.50 s | 通过 |
| 原手动方案无故障基线回归 | 40 / 0 | 未采集 | 通过 |

Ubuntu 正向组均保持同一 systemd/PID 1、journald、D-Bus 和被观测实验服务实例，无 failed units；根文件、块读取、写入/fsync 和确认数据前缀检查均通过。等待时间包含驱动、枚举、核验和调度开销，不是严格的物理拔插时长。

真实 Linux v6.8 Git 场景中，基线约 84.59 秒完成，初轮故障组约 91.41 秒完成，两组均通过最终验收。故障发生在接收对象约 6% 时，原 Git 进程继续并退出 0；83,475 个文件检出完成、HEAD 匹配 `e8f897f4afef0031fe618a8e94127a0934896aba`、`git fsck --full` 成功且工作树干净。伴随 fsync 负载分别成功 735 / 799 次、失败均为 0，心跳最大间隔 0.96 / 1.35 秒。

该初轮故障组设定间隔 0.2 秒，但删除确认到重接完成实为 1.39 秒，原因是串口观测串行占用了时间；不能把它当作 0.2 秒实测。两个 VM 同时运行，上述总耗时也不能直接相减作为断联成本。

独立计时补测通过全部验收：删除确认后 0.200 秒发出重接请求、0.317 秒完成 QEMU 重接操作、1.759 秒时主机观测到路径恢复。原 Git 进程约 47.02 秒完成克隆并通过完整性检查；伴随写入 519 次成功、0 次失败，最长等待 1.06 秒，心跳最大间隔 0.76 秒。shell 探针在重接请求前已完成。这轮单 VM 运行，总耗时不能与前面的双 VM 并行组直接比较。

0.2 秒是人为注入参数，可设得更短（`--gap 0` 或 `--gap 0.01`），不是恢复机制的固有门槛。QMP 操作、设备重新枚举、核验及观察都有额外开销；完整移除/重建设备也不同于驱动尚未移除设备时的极短电气抖动。上述数据不能作为真实 USB 链路抖动的精确时序。

调试中两轮 Git 虽然通过应用检查，但大型未压缩报告使主机接收心跳间隔约 2.9 秒，故整体验收判为失败；这些失败记录单独保留。新增串口压缩后重新验收，没有放宽 2 秒门槛。原有 13 项恢复单元测试及新增 3 项启动观测/串口协议回归通过。

完整摘要、验收项及来源/代码/镜像哈希见 [results/2026-09-24-ubuntu.json](results/2026-09-24-ubuntu.json)。

## 零额外等待与同端口重接补测（2026-09-24）

```bash
python3 lab/auto_run.py --guest ubuntu --workload git-clone --gap 0 --cycles 3
python3 lab/auto_run.py --guest ubuntu --workload git-clone --same-port --gap 0 --cycles 3
```

`--same-port` 将初始设备及每次重接固定到虚拟 USB 端口 1。未指定时沿用 QEMU 自动分配端口；这次默认组依次出现 `usb 2-1` 到 `usb 2-4`，固定端口组始终是 `usb 2-1`。两组均在 Ubuntu 的 LVM 根卷上执行真实 Linux 源码克隆及持续写入/fsync。

| 场景 | 写入成功 / 失败 | 最长写入等待 | 最终验收 |
|---|---:|---:|---|
| 自动分配端口，三次零等待重接 | 416 / 0 | 1.217 s | 全部通过 |
| 固定同一端口，三次零等待重接 | 449 / 0 | 0.897 s | 全部通过 |

六次均在删除确认后约 0.4–0.7 ms 发出重接请求，约 116–117 ms 完成 QMP 重接操作；这些不是 guest 完成枚举或恢复读写的时长。固定端口组内核记录的断联到新 SCSI 磁盘出现仍约 0.51–0.56 s。

两组底层分区均按 `sda1 → sdb1 → sda1 → sdb1` 变化。全部八个挂载观测点（故障前、每轮两次、最终）中，根挂载行保持不变：mount ID 27、设备号 `252:1`、源 `/dev/labrescue/ubuntu`、ext4 可读写。根 LV 的 linear 映射持续指向稳定的 `252:0`；只有保护层的底层路径被替换。挂载参数 `errors=remount-ro` 是错误处理策略，并不表示已进入只读。

原 Git 进程均完成克隆，83,475 个文件、HEAD、`git fsck --full` 和干净工作树检查通过；原写入进程、systemd、被观测服务实例及 boot ID 保持不变，确认写入的数据回读通过。固定端口组第三次还记录到磁盘出现但目标分区尚未就绪，Guard 拒绝切换，待校验通过后恢复。

这证明预置保护层的 LVM 根系统能在上述快速重枚举中保持原挂载和应用存活。普通 LVM 本身没有这里的排队保护；不能直接推断未经改造的实际根盘也会如此。没有跟踪旧 gendisk 最终释放和所有未完成请求的收尾时刻，因此尚不能宣称精确复现了旧、新内核对象的生命周期交叠，或覆盖全部竞态。QMP 删除确认也不等于 guest 所有清理已结束。

原始日志在对应 `lab/work/` 运行目录，公开摘要保留报告哈希、载荷哈希、挂载观测、DM 表、内核及 Guard 事件：[快速重接结果](results/2026-09-24-fast-reconnect.json)。

## 旧轮询版 Guard 健康状态 CPU 基线（2026-09-24）

在双核 Ubuntu guest 内，健康路径无拔盘、未启动实验写入负载时，连续采样三个 10 秒窗口。读取 Guard 的 `/proc/PID/stat`，分别计算自身 `utime+stime` 与已回收子进程 `cutime+cstime` 增量，按 guest `CLK_TCK=100` 换算；100% 表示占满一个虚拟 CPU。

| 窗口 | Guard 自身 | 子进程 | 合计 |
|---|---:|---:|---:|
| 第一个 10 秒 | 1.80% | 3.80% | 5.60% |
| 第二个 10 秒 | 1.80% | 4.00% | 5.80% |
| 第三个 10 秒 | 1.80% | 3.70% | 5.50% |

加权平均为单核的 5.63%，相当于这台双核 guest 总 CPU 容量的约 2.82%；父进程 RSS 为 13.25 MiB。测量期间 Guard 始终 ready、恢复次数为 0。健康轮询约每 100 ms 启动一次 `dmsetup status`，进程创建和命令执行是明确的优化对象；仅查看父进程 CPU 会低估开销。健康轮询不会执行恢复阶段的 PV/LVM 身份核验。

这是当前 KVM 环境的一次 30 秒采样，不是实机长期性能保证，也不包括 DM 数据路径、其他内核工作线程或瞬时子进程内存。没有测量故障恢复阶段的 CPU 峰值。这组记录是替换监视实现前的基线，下面另列优化后的结果。

测量命令完整输出后，宿主包装脚本因结果前带 shell 提示符而误判超时；从保留的原始输出提取了全部三个窗口，并修正本地脚本的提取规则。数值与测量边界见 [CPU 采样记录](results/2026-09-24-guard-cpu.json)，本地复现脚本为 `lab/work/measure_guard_cpu.py`。

## 事件监视替换与 CPU 配额验收（2026-09-24）

现已替换原 100 ms 命令轮询：内核 netlink 事件唤醒、1 秒兜底、进程内 libdevmapper 状态查询、重复事件合并及 100/200/400/800 ms 串行退避。先检查 sysfs 和节点就绪再读候选盘元数据，完整身份核验及切换前复核保留。故障判定条件没有改为 I/O 积压推断，也没有添加主动健康读盘探针。实现细节见 [自动恢复机制](AUTOMATIC.md)。

```bash
python3 lab/build.py --kernel lab/work/kernel-package/boot/vmlinuz-6.8.0-139-generic
python3 lab/measure_guard.py
python3 lab/auto_run.py --guest ubuntu --workload git-clone --same-port --gap 0 --cycles 3
```

`measure_guard.py` 自动创建并关闭独立 Ubuntu VM，通过 Guard cgroup 的微秒级 `cpu.stat` 每约 100 ms 采样，包含所有子进程。采样器与合成事件发送者位于其他 cgroup。先测 30 秒健康空闲，再向 guest 的 DM 设备写入约每秒 100 条合成 change 事件，持续 10 秒；不拔盘、不修改映射。这是事件风暴及误触发验证，不是另一种故障注入。

| 场景 | 单核平均 CPU | 100 ms 窗口峰值 | P95 |
|---|---:|---:|---:|
| 健康空闲 | 0.104% | 1.512% | 0.998% |
| 约 100 条事件/秒 | 0.889% | 2.181% | 1.374% |

两阶段 Guard 都维持同一实例、ready、0 次恢复，子进程 CPU ticks 没有增加。父进程 RSS 约 14.63 MiB。配额实测为 `4000 20000`（单核 20%、20 ms 周期）；采样阶段 throttled 计数没有增长，因此平均下降不是靠限额强行压住忙轮询。此结果来自一次 KVM 采样，不能作为所有硬件上的峰值保证，也不能排除小于采样窗口的瞬时运行。

恢复回归：完整 Ubuntu/LVM 根卷、真实 Linux Git 克隆、同端口三次零额外等待重接全部通过，223 次写入成功、0 次失败，最长等待 1.197 s，Git 完整性、进程和被观测服务存活均通过。最小 guest BOT 三次重接通过，67 次写入成功、0 次失败，最长等待 1.840 s。20% 配额在这些样本中足以在原等待窗口内恢复，未宣称它是最优配额。

负向回归：无盘返回与错误 PV 都在约 4 秒窗口释放等待；杀死 Guard 后，内核 6 秒兜底生效，最长被测写入等待 6.191 s。负向组的应用错误是预期结果，RAM 救援仍可用。19 项单元测试通过，包含事件不能绕过退避、事件合并/周期兜底以及分区未就绪不读盘。

早先 CPU 包装脚本的长串口命令没有获得有效采样；当前工具改为短命令分段写入 RAM 文件后执行。有效样本和五组恢复回归的哈希、配置及检查项保存在 [监视优化验收记录](results/2026-09-24-monitor.json)。生产主机的根盘布局与已安装救援服务没有部署本轮实验变更。
