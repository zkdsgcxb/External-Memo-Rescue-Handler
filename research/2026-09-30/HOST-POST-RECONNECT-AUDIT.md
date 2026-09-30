# 首次实机拔插后的全面检查报告

日期：2026-09-30，Asia/Shanghai。对象：当前 Ubuntu `7.0.0-34-generic`（启动日志标识上游基础 `7.0.14`）、外置 USB SSD、现有 LVM/ext4、保护启动入口。源码版本 `f1351b9`。本报告是一次实机事件及其事后检查，不是全盘无损、全部应用无错或长期可靠性的认证。

**结论：本次根卷和 shared 卷的短断恢复成功，原系统没有重启，两个原测试进程无错误继续读写。但仍有两项必须处理：USB EFI 未恢复挂载且有 dirty 状态；保护启动期间出现了块层合并 WARNING，原因尚未闭环。因此本轮为“核心恢复通过，整体验收有保留”，不建议继续重复拔插或直接认定可无条件长期使用。**

检查以读取现有状态、日志、元数据及测试数据为主。没有再次故障注入、清缓存、USB reset、LV refresh、修改 DM、改内核参数或运行修复型 fsck。唯一临时挂载是对未挂载 EFI 的独立 `ro,nosuid,nodev,noexec` 检查挂载，结束后已卸载；没有恢复 `/boot/efi` 的业务挂载。

## 检查结果总表

| 对象 | 证据 | 结论 |
|---|---|---|
| 启动入口 | cmdline 含 `ram_rescue_guard=1 nompath noresume`；固件 BootCurrent 指向内置 rEFInd | 保护入口实机启动通过 |
| 稳定映射 | `ram-rescue-path` 保持 `252:0`，后端从 `8:3` 换至 `8:19`；无 suspended/inactive 残留 | 本次换表完成 |
| 根卷/shared | 分别为 `252:1`、`252:2`，均仍依赖 `252:0`；两卷 ext4 均 rw | 上层映射及挂载保持 |
| Guard | 原 PID 683、原 owner epoch，`ready`、`path_restored`、recoveries=1；探测 errno=0 | 自动恢复与内核探测确认完成 |
| 原测试进程 | PID 5645、5646 的启动标识保持，拔插后继续产生 ACK；之后主动停止负载 | 无进程重建 |
| 写入结果 | 每卷 894 次成功、0 次错误；最大写入/fsync 停顿 5.4001 / 5.3989 秒 | 两个测试负载通过 |
| 数据回读 | 每卷 3,665,292 字节全部匹配；另以 `dd iflag=direct` 绕过主机页缓存复核，退出 0、内容完全匹配 | 已确认测试数据通过直接回读 |
| ext4 | 两卷 `errors_count=0`、`warning_count=0`，首末错误字段为 0/空；未见 ext4 error、journal abort | 未观察到两卷文件系统错误 |
| EFI | 系统主动卸载 `/boot/efi`，重接后未重挂；只读 FAT 检查返回 1、dirty bit、主备启动扇区差异 | 未恢复，需处理 |
| EFI 启动程序 | 临时只读挂载可读 105 个文件；shim、GRUB、MokManager 与本机包内文件哈希一致 | 未发现这些启动二进制损坏 |
| EXCHANGE | 已自动重新挂载到新 `sdb2`，仍为 exFAT/rw | 恢复了挂载，未证明原文件句柄及数据连续性 |
| RAM 救援 | prepare、日志、tty9、tty10 均 active；tmpfs 带 noswap；用户已实际登录 F9 | 入口正常 |
| 系统/用户服务 | 两个 systemd 管理器均无 failed units；系统 running；包审计无输出 | 当前无残留失败单元/半配置包 |
| 内核 | 一条块层 WARNING；taint=512；未检出 OOM、hung-task、journal abort | 内核警告仍待定位 |

`dumpe2fs -h` 同时显示两卷 state=clean 与 needs_recovery；它们正在挂载并使用 journal，不能把这些字段当成离线 fsck 通过或损坏证据。配置的 Errors behavior 为 Continue，所以“仍 rw”本身不充分；这里结合错误计数、日志、实际 fsync 与直接回读判定。

## 事件时间线

以下统一使用启动后的单调秒，避免 journal 墙钟显示与实际事件时间混淆。

| 时间 | 事件 |
|---:|---|
| 4.407 | Guard 初始 ready，旧后端 `/dev/sda3` |
| 42.163 | `blk_rq_set_mixed_merge` WARNING，发生于 ChatGPT 进程的读取/预读调用上下文 |
| 507.632 | 旧 sda 两个写请求返回底层 I/O error，DM 标记路径失败 |
| 507.648 | USB disconnect |
| 507.668 | Guard waiting，准入 deadline=515.667 |
| 507.678 | systemd 开始卸载 `/boot/efi` |
| 507.685 | FAT 无法从已经消失的 sda1 读取 boot sector 以标记状态 |
| 511.745 | 同接口的新 SuperSpeed USB 枚举开始 |
| 512.847 | Guard 核验新 `/dev/sdb3`，进入路径加载 |
| 513.033 | `/boot/efi` 卸载完成；随后 EXCHANGE 自动挂载到 sdb2 |
| 513.108 | Guard probing |
| 513.185 | Guard ready，recoveries=1，内核探测耗时约 15.85 ms |

断联到新 USB 枚举开始约 **4.097 秒**；waiting 到 ready 约 **5.517 秒**；ready 距准入期限剩余约 **2.482 秒**。实际应用最长停顿约 5.4 秒。不能把这次物理实验写为“0.2 秒拔插”，也不能仅凭日志推断用户手部动作耗时。该次约 36 ms 的失效检测延迟只是一个样本，不是响应保证。

两个底层 I/O error 与“测试应用零错误”并不矛盾：旧设备先报告失败，预置 DM 层随后排队、换到核验后的新路径，使本次受测请求继续完成。没有宣称内核日志全程零错误，也没有证明全部应用均无错误。Guard 对上层错误历史仍诚实保留 `incomplete`。

## 需要处理的两项问题

### 1. EFI 状态与挂载恢复缺口

`/boot/efi` 指向 USB SSD 的第一分区；内置 SSD 的 rEFInd 不是这个 FAT 分区。当前仅第三分区上的 LVM PV 经稳定映射保护，第一分区和 EXCHANGE 未受同样保护。

systemd/udisks 日志证明：设备消失后卸载发生，重连后 EXCHANGE 被重新挂载，而 `boot-efi.mount` 留在 inactive/dead、Result=success。因此“系统没有 failed units”不等于 EFI 已恢复。

对**未挂载** EFI 执行 `fsck.fat -n -v`：返回 1，报告 dirty bit；主/备 boot sector 在偏移 65 为 `01/00`；检查了目录、未使用簇和空闲簇摘要，未报告交叉链、丢失簇或文件链损坏。不能将这缩写为“EFI 完全正常”，也不能仅凭 dirty bit 推断启动文件已损坏。

工具输出虽含 “Automatically removing dirty bit”，但此次指定 `-n`，末尾明确 “Leaving filesystem unchanged”；**实际没有修复**。[Ubuntu fsck.fat 手册](https://manpages.ubuntu.com/manpages/noble/man8/fsck.fat.8.html)明确说明 `-n` 不写文件系统。

只读挂载下，三个关键 EFI 二进制与已安装软件包相同；EFI 的 GRUB 配置仍搜索原根文件系统并加载 `/boot/grub/grub.cfg`。保护 initrd 与 GRUB 主配置也匹配安装记录。没有执行实际固件重启验收，因此这些检查不等于保证下次引导成功。

**处理顺序：** 先保留 EFI 内容及只读检查记录，在未挂载状态下针对已识别问题做最小修复并复查，再恢复正常挂载；之后补充原盘重接后 EFI 的受控恢复策略。修复前不进行依赖 EFI 写入的引导器/系统升级。不能简单对任意返回盘自动 mount，更不应不经审查把所有修复交给 `fsck -y`。

### 2. 块层 mixed-merge WARNING

警告发生于开机约 42 秒，早于拔插约 465 秒。上一轮普通入口使用相同 `7.0.0-34-generic`，保留日志中没有同一警告；但两次工作负载不同，不能单凭一次对比证明保护层或新内核是唯一原因。

栈包含 `ext4_readahead → submit_bio → blk_mq_submit_bio → bio_attempt_back_merge → blk_rq_set_mixed_merge`。进程名 ChatGPT 表示触发读取时所处的执行上下文，**不是证明 Codex 代码损坏了磁盘**。

上游对应函数检查合并请求与各 bio 的 `REQ_FAILFAST_MASK` 是否一致；BIO multipath 会添加 `REQ_FAILFAST_TRANSPORT`，预读路径也参与 failfast 标志转换。因此“BIO multipath、预读、请求合并之间的标志处理”是有源码依据的调查方向，**仍是推断**。本次未取得逐行匹配 Ubuntu 构建的完整源码/调试符号，也未复现或确认修复补丁，不能声称已找到唯一根因。[stable 7.0.14 合并代码](https://raw.githubusercontent.com/gregkh/linux/v7.0.14/block/blk-merge.c)、[multipath BIO 映射](https://raw.githubusercontent.com/gregkh/linux/v7.0.14/drivers/md/dm-mpath.c)。

当前 taint=512 对应 W（内核曾发出警告），不是自动等同于 kernel panic 或介质损坏。[Linux taint 定义](https://docs.kernel.org/admin-guide/tainted-kernels.html)。该检查使用 WARN_ON_ONCE；因此没有继续打印同一警告，不能证明相关触发条件不再发生。

**处理顺序：** 保留本轮完整调用栈；在可丢弃 VM 上对照当前 Ubuntu 内核、BIO multipath 与预读/合并工作负载，再核对对应发行版补丁。暂不在真实根盘上用关合并、关预读等参数掩盖警告，也不因猜测立即混装内核。

## Guard 的实机日常开销

恢复后单独采样约 30.27 秒，每约 100 ms 读取 Guard cgroup；包括其子进程，没有注入事件风暴或再拔插。

| 指标 | 实测 |
|---|---:|
| 平均 CPU，一个核心=100% | 0.1587% |
| 约 100 ms 窗口最大 CPU | 2.2721% |
| cgroup memory.current | 15.16–15.66 MiB |
| 最大进程数 / 线程数 | 1 / 1 |
| 新增 CPU 节流次数 | 0 |
| CPU 配额 | 4 ms / 20 ms，即一个核心的 20% |
| 内存 / swap 配额 | 128 MiB / 0 |

memory.current 包含该 cgroup 被计费的内存，并非 Python 独占 RSS，也不是完整共享 RAM 工具环境的总量。此短窗支持“本次健康期控制面开销较低”，不证明不存在更短瞬时峰值、长期泄漏或高负载问题；CPU 配额也不封顶内核工作线程及全系统开销。

系统约有 26 GiB 可用内存，8 GiB swap 当前使用 0；swapfile 位于受保护根卷，而 Guard/RAM 工具自身禁止 swap。没有进行内存压力、swap 密集、休眠恢复测试。

## 其他检查与边界

- 当前 Wayland 会话 active，网络 connected/full；PipeWire、pipewire-pulse、WirePlumber active。这是状态检查，没有新增图形、音频、GPU 压力测试。
- ACPI 缺失对象及 64 条 nouveau 控制命令失败在上一轮普通启动也存在，不能算成本次拔插新引入。没有因此将这些旧问题判为无害。
- 一次进程快照出现 i915 flip 内核工作线程处于 D 状态；未见存储相关用户进程持续 D 状态或 hung-task 报警。瞬时 D 状态不能单独判定永久阻塞。
- `dpkg --audit` 无异常；日志中未检出本次应用 segfault、OOM 或实际 coredump 事件。系统未提供 coredumpctl，所以没有完整转储目录审计。
- USB bridge 当前未暴露可用的 UDisks ATA SMART 接口，主机未安装 smartctl。**没有得到 SSD 寿命、介质坏块、温度、掉电计数的可靠 SMART 结论。** 没有全盘扫描或硬件自检。
- 未对在线根卷/shared 运行 e2fsck；错误计数为零和约 7.33 MB 测试数据通过，不能替代两个完整文件系统的离线一致性检查。
- F9 的人工登录发生在拔插前；本次没有在实际断联期间交互操作 F9，因此不把登录成功扩大为故障窗口内的终端响应验收。
- `arch` LV 的元数据仍存在，但本次未激活/挂载，不纳入验收。EXCHANGE 的原打开文件句柄、未保存数据、其他应用内部状态均未覆盖。
- 本轮没有验证长期断联、多次连续拔插、USB Hub、电源抖动、设备缓存断电耐久性及任意应用超时；这些不因一次成功样本自动成立。

## 后续优先级

1. **先收尾 EFI：** 备份、最小修复、复查、恢复挂载，然后补齐它在重接后的处理策略。
2. **定位内核 WARNING：** 对应 Ubuntu 源码与 VM 复现；避免在实机反复拔插试错。
3. **保留本次已通过合同：** 稳定 DM、唯一 owner、身份核验、现有原生探测、RAM 救援继续作为当前基础；没有证据要求重写 Guard 或改用 eBPF。
4. 前两项闭环后，再在可保存/可恢复的工作负载下增加实机次数。当前不将整体状态标为“全部通过”。

本次只检查并报告，没有擅自修复 EFI、改变默认启动项或升级内核。

## 可追踪证据

原物理实验位于 `lab/work/physical-20260930-193426/`；全面检查位于 `lab/work/physical-check-20260930/`。这些目录被 Git 忽略，含私有设备标识及原始日志，不随报告公开。目录内 `SHA256SUMS.json` 给出采集文件哈希。

| 文件 | SHA256 |
|---|---|
| `root-inspection.json` | 59cf023bf08b87e28daabeb974942749a0fa24ccfa14e3abbb90d306d37a6fe4 |
| `efi-files.json` | 1b22d9274eefdeba38b8215ec583c5a4a33512518e9013d10b5a7ff70cadeb66 |
| `direct-read.json` | 5718ea87fca890e05ea48182598b873f5e3b0c56ff70c2ebee1484cf0761376c |
| `guard-budget.json` | c5e29f80ac870f775e88610a1611284d2baee74522996a425a8bfda3a2006630 |

采集脚本、原始命令、返回码、完整日志、映射/元数据/服务状态、FAT 只读检查输出和采样数据均保留。报告中的“通过”只对应表中明确列出的实测项。
