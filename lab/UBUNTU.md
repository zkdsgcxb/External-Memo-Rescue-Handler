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
