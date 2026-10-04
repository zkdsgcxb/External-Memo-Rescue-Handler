# 原生运行时与服务权限收缩

2026-10-04。本文记录 ROADMAP P1 的原生实现和本地验收；systemd 策略已结合本轮完整 QEMU 报告验收，实机启用仍需另行记录。结构化证据见 [native-security.json](../../lab/results/2026-10-04-native-security.json)。

## 信任路径与输入

运行入口的登记、配置和运行时收据/事务使用 `openat` 持有的目录 FD 从 `/` 逐组件打开：每个父目录必须归 root 所有，且不能被组或其他用户写入；不接受符号链接、`.`、`..` 或 NUL。叶文件必须是同样权限要求的普通文件，且硬链接数为 1。`O_NONBLOCK` 防止伪造 FIFO 在类型检查前阻塞入口。普通 `/run`/tmpfs 可用；`/tmp` 即使设置 sticky bit 也不作为可信父路径。

新状态目录通过 `mkdirat` 建立后继续校验，不对已有的不安全目录直接 chmod 来掩盖风险。状态发布使用持有的父目录 FD、随机 `O_EXCL` 临时文件和 `renameat`；已有符号链接、硬链接、非普通文件或可写文件被拒绝，拒绝时旧记录保持原状。Owner 的 flock 描述符、异步 worker/helper 的继承及事务先后顺序保持原有语义。

配置/状态校验没有加入常态每秒的路径轮询；它们发生在入口、接管读取以及实际证据发布时。sysfs/procfs 的设备观察继续使用原读取接口，不将控制文件的信任规则错误套到内核符号链接或设备信息上。实验 profile 仍同时要求 `ram_rescue_lab=1` 和 QEMU DMI 标识；不能用 JSON 内的 lab 字段绕过生产入口的文件信任检查。

JSON 最多 64 层，控制记录最多 64 KiB，helper JSON 最多 1 MiB；重复对象键会被拒绝。blkid 重复键或 NUL 会被拒绝，不能靠覆盖先前字段改变身份结论。事件仅提供唤醒提示；运行时仍只接受内核 netlink 发件者，恢复前仍经过原设备身份、活 FD、diskseq 与切换前最终核验。序列号和 UUID 可复制，本实现并不提供硬件真实性认证。

## 权限边界

| 执行阶段 | 必要权限与限制 | 保留原因与残余风险 |
| --- | --- | --- |
| initramfs 早期激活 | root；允许已有 mount/chroot、设备节点和 LVM 激活流程 | 必须建立真实 `/dev`、`/proc`、`/sys`、共享 `/run` 与初始稳定 DM；不直接套用常驻服务的禁止 mount 规则 |
| 常驻根盘/登记数据盘 C++ | `NoNewPrivileges`，仅 `CAP_SYS_ADMIN`、`CAP_DAC_READ_SEARCH`、`CAP_MKNOD`、`CAP_CHOWN`；只允许 AF_UNIX/AF_NETLINK、本机 syscall ABI | DM 控制/probe ioctl、受限路径读取、libdevmapper 节点管理仍需要权限；CAP_SYS_ADMIN 较宽，不能据此宣称已成为无特权服务 |
| 只读介质 helper | 继承常驻服务的 cgroup、capability 上界、syscall/地址族和文件系统限制；空 stdin、固定环境、绝对工具路径、输出/截止时间约束、保留 flock FD | 现有 blkid/LVM 工具通过原路径打开 root 管理的设备，LVM 使用 `/run` 锁；没有引入第二个 daemon 或自写工具。未完成单独降 UID/按 FD 代理，helper 仍共享 CAP_SYS_ADMIN 残余权限 |
| RAM 内核日志 | 独立进程仅保留 `CAP_SYSLOG`、`NoNewPrivileges`；日志目录设置显式可写例外，限制 namespace、syscall、地址族 | 初始 dmesg 与 `/dev/kmsg` 读取需要 SYSLOG；不继承 Guard 的 DM 能力，不隐藏 kmsg。仍以 UID 0 运行；本轮完整 VM 已验收启动快照与重连后持续记录 |
| F9/F10 人工救援 | 经 BusyBox 登录认证后完整 root；沿用 P0 会话退出规则 | 用户主动授权的系统救援可能需要 mount、修复与诊断，不能套用禁止这些操作的自动 Guard 规则 |

[security_policy.py](../../guard/security_policy.py) 是根盘和数据盘 renderer 共用的限制定义。静态根盘 unit 使用相同文本；限制包括禁止 mount/模块加载/reboot/swap/raw I/O/ptrace 调试/时钟修改等 syscall 组，拒绝新建/切换 namespace、SUID/SGID 创建、实时调度与动态可写可执行内存。`ProtectSystem=strict` 将普通程序和配置目录设为只读，并为 `/run`、`/dev` 设置写入例外，供共享锁、事务和设备节点处理；内核和 home 例外见下文，没有 `PrivateDevices`。本轮 QEMU 已在实际命名空间中核对挂载属性并完成重连；unit 文本本身不构成恢复成功证据。

`/run`、`/dev` 仍是宽于单一映射的可写范围；用户态文件策略防止非 root 注入，capability 与 syscall 规则缩小误用空间，但都不是对已经控制 root/CAP_SYS_ADMIN 攻击者的完整隔离。本轮不增加独立特权代理或新的控制端口。

UID 0 与共享 `/run` 还保留访问宿主 Unix 管理 socket 的可能；因此 capability 上界不能被解释为对失陷服务的完整权限沙箱。当前职责是缩小直接内核接口、输入与文件写入面，并让恢复流程可审计；隔离宿主控制 socket 或拆分特权代理需要另行设计，不能由本轮只读挂载验证推出。

`ProtectSystem=strict` 自身保留 `/proc`、`/sys`、`/dev` 等内核 API 文件系统，以及由 `ProtectHome` 独立管理的 `/root`、`/home`、`/run/user` 例外。本轮没有设置 `ProtectHome`，所以不能把它等同为整个命名空间只有显式 `ReadWritePaths` 可写。常规 `/etc`、`/usr` 程序与配置的实际写拒绝，以及需要写入的状态/日志路径，须分别验收；根服务的拒写证据也不能冒充每个独立数据服务都执行过同一个写探针。

`ReadWritePaths` 明确写成 `+/run +/dev`，日志 unit 为 `+/var/log`：systemd 的 `+` 表示相对 `RootDirectory`，不能省略。此前未加 `+` 的候选在真实 VM 中使日志目录仍只读；宿主 `/run` 还可能把其下整棵 RAM 环境纳入写入例外。它不满足预期隔离，已替换，不能根据早期 unit 属性读回宣称限制有效。最终 fixture 已从各真实服务进程的 mountinfo 检查根挂载只读、要求的状态/设备/日志挂载可写；这一检查没有逐目录执行写入探针，不能扩展成所有子目录都通过实际写拒绝验收。

### systemd 根目录必须使用真实挂载路径

完整 Ubuntu 验收发现，`RootDirectory=/run/ram-rescue-manager/tools` 使用符号链接时，会在 C++ 执行前因 `226/NAMESPACE`、只读 remount 返回 EBUSY 而失败。对照报告 `lab/work/sd-1004-221724-284667/report.json` 的 `namespace_comparison` 保留了同一生产限制运行 `/bin/busybox true` 的结果：alias 返回 226，真实 `/run/ram-rescue-manager/rootfs` 返回 0；并保存 debug 日志。

上游 [systemd v255.4 mount-util.c](https://raw.githubusercontent.com/systemd/systemd-stable/v255.4/src/shared/mount-util.c) 的递归只读 remount 按 mountinfo 路径前缀匹配，找不到前缀时重新 bind 并扫描，32 轮后返回 EBUSY。这与别名不出现在 mountinfo 中的现象一致；对照实验验证了本项目的触发条件，并未宣称已证明所有版本的内部执行轨迹。当前服务统一使用真实 PRIVATE 挂载路径；复用保护根盘 RAM 环境时通过 bind 共享已有文件，管理别名不作为服务根目录。全部 capability、syscall 和 `ProtectSystem=strict` 限制保留；最终恢复验收另列。

### fstab 的冷态准入视图

缩减 capability 后，数据盘 C++ 不能再通过 `/proc/1/root/etc/fstab` 解引用 PID 1 的根目录；这受进程访问检查约束，不能靠 DAC 读取权限解决。本轮不增加 `CAP_SYS_PTRACE`，也不暴露整个宿主 `/etc`：冷态 RAM 准备将一个 `fstab` 文件只读 bind 到 PRIVATE 的 `/etc/rescue/host-fstab`，C++ 只在启动准入读取它，原有 UUID、label、raw partition 拒绝条件不变。

绑定保留冷准备时的 inode。对原文件的原位编辑仍可见；若管理员原子替换 `/etc/fstab`，管理入口再次准备时发现 inode 不同会拒绝并要求新 boot，不在线覆盖已有挂载。udev 自动启动使用最近的冷态基线，不能宣称它总会反映新替换的 fstab inode。运行中的挂载状态仍通过 `/proc/1/mountinfo` 读取。此选择使 `ExecStopPost` 继承已准备的子挂载，无须在根盘故障时重新解析宿主 `/etc/fstab` 绑定源；systemd unit 本身不设置这个额外 `BindReadOnlyPaths`。

## 二进制与 CI

发行构建显式使用 `-fPIE -pie -fstack-protector-strong -D_FORTIFY_SOURCE=3` 和 `-z relro -z now -z noexecstack`，并通过 readelf 检查实际精简 ELF 的 DYN 类型、GNU_RELRO、NOW、无执行栈、栈检查及 fortified 调用符号。构建清单记录参数、源码与 ELF 散列；仅检查编译命令不算验收。

[CI](../../.github/workflows/native-security.yml) 在发行和 ASan/UBSan 两种构建下执行同一原生测试、ELF 检查与每种构建 10,000 次确定性输入变异。本地对应命令通过后，提交 `25b76b7be8fea3d5a5279cd00f541e36f972bfa7` 的 [GitHub 远端任务 37214536482](https://github.com/zkdsgcxb/External-Memo-Rescue-Handler/actions/runs/37214536482) 也已完成，两种构建均成功；[任务记录](../../lab/results/2026-10-04-remote-ci.json) 保留提交、workflow 散列和各 job 时间。此 CI 结果不替代特权 QEMU 或实机启动验收。checkout 固定到上游 [v4.2.2 的具体提交](https://github.com/actions/checkout/commit/11bd71901bbe5b1630ceea73d27597364c9af683)，凭据不写入工作区，workflow 仅有 contents:read 权限。宿主编译器和系统库版本仍随已登记构建环境变化，不声称源码散列即发布者认证。

GCC 13 在 sanitizer 编译 libstdc++ `std::regex` 时产生内部 `maybe-uninitialized` 告警。仅 sanitizer 构建保留其可见输出但不将该类告警升级为错误；发行构建继续 `-Werror`。ASan/UBSan 是离线检查，不进入生产 RAM 包。`stage_runtime()` 在复制前检查动态库闭包，拒绝 libasan/libubsan 依赖（同时检查链接路径和解析后的文件名）；这是对本项目动态 sanitizer 构建的防误部署检查，不是任意 ELF 的源码认证。

## 本地结果与限制

- 143 项原生检查通过：admission 37、controller 13、core 60、存储/输入安全 33。
- 两种构建各完成 10,000 次固定种子变异，覆盖 JSON/登记/事件、DM 表、blkid/LVM 输出；ASan/UBSan 未报错。
- 两种 ELF 均通过 6 项硬化验收。发行精简 ELF 为 806,240 字节；这只是文件大小，不能推导常态或恢复 CPU、RSS/PSS 不变。
- UID/权限/类型的组合由纯元数据用例覆盖；普通用户测试还实际拒绝可写 `/tmp` 父路径、`/proc/self` 符号链接、路径穿越和状态文件替换。实际 root 管理的生产配置的正向路径由完整 VM 启动验收。
- 源码、编译信息与原始日志位置见结构化报告。单元/变异测试不接触块设备、不执行实机部署，不代替完整故障/接管矩阵，也不证明不存在解析器缺陷。

## 完整 Ubuntu 运行证据

- `lab/work/cpp-int-1004-224707-476521/report.json`：39/39，通过保护根盘加两个登记数据盘的实际生产 owner、精确 unit 字节与无 drop-in 核验；三个 controller 的实际根挂载只读，状态 `/run` 与 `/dev` 可写。独立 RAM 日志使用 CAP_SYSLOG，启动快照有内容，恢复后进程继续记录，日志 `/var/log` 挂载可写。
- `lab/work/sd-1004-232607-652502/report.json`：42/42，普通 Ubuntu 两次冷启动、两个数据 owner 与每次实际 namespace 属性通过，最终安装入口登记、私有文件权限/散列及导出也纳入该轮；同名重复登记拒绝由普通回归覆盖，未在该 VM 场景重复执行。未用别名 RootDirectory，也未增 CAP_SYS_PTRACE 或放宽 capability/syscall 策略。
- 上述报告确认实际进程挂载属性与功能存活；未逐项写入 `/etc`、`/usr` 等子目录，不构成完整失陷进程沙箱证明。事务死亡与超时的独立矩阵见 [TRANSACTION-POLICY.md](TRANSACTION-POLICY.md)，性能成本见 [PERFORMANCE-METHOD.md](PERFORMANCE-METHOD.md)。
