# 部件、实现来源与替换边界

完整 Ubuntu Server 与 Git 克隆集成见 [lab/UBUNTU.md](lab/UBUNTU.md)。

本文区分手动 RAM 救援与自动保护：`ram-rescue-demo/` 提供手动工具；`guard/native/runtime/` 是新的 C++ 自动恢复核心，`guard/runtime/` 保留 Python 行为对照；`lab/` 负责构建虚拟机、注入故障和独立验收。根盘保护入口已实际启动，2026-09-30 首次实机短断测试通过，内核 WARNING 仍待定位。2026-10-01 增加独立 USB 数据分区实验入口，复用相同控制器，见 [数据映射接入](guard/DATA.md)。源码更新不等于更新已安装包。

当前只维护 Linux **7.0.0-34-generic / x86_64** 基线，要求 multipath target ≥ `1.15.0`。实机入口额外要求 `ram_rescue_guard=1` 启动标记与登记的内核 release 完全相符；没有旧内核兼容或无探测降级分支。

这里的“我们实现”指本仓库新增的程序、策略和集成；“上游实现”指 Linux、LVM2、BusyBox、systemd、QEMU 等现有开源项目。两者并不矛盾：一个部件可以由我们编排，实际机制由上游提供。

用户入口现已统一为 `rescue-guard`，自动识别已有根盘 owner 与普通文件系统登记；根盘和数据分区属于内部接入、核验策略的区别。`registry.py` 负责冷态登记，C++ `maintain` 入口核验当次实例后进入同一恢复循环；udev + systemd 在登记的稳定 DM 映射出现时启动服务。准备服务只运行一次，不增加常驻轮询总管，详见 [统一后台维护](guard/MANAGER.md)。

## 现有功能具体怎么做到

技术栈以 **C++17 运行时、Python 3.12 管理与实验工具、POSIX Shell、systemd、Linux Device Mapper/LVM2/ext4、QEMU/KVM** 为主。没有 Web 服务框架或数据库依赖，也没有 eBPF 程序、自编内核模块。JSON 用于登记、控制消息和实验记录；原生单测和 Python unittest 验证策略，QEMU 验证真实故障。C++ 迁移与完整运行时比较见 [本轮报告](research/2026-10-02/CPP-MIGRATION.md)，源码更新尚未替换本机运行包。

按一次故障的时间顺序看：

1. **故障前准备工具。** `build.py` 收集 BusyBox、Python、LVM 等二进制及依赖，生成工具包。`prepare.sh` 将包展开到 `tmpfs,noswap`，接入 `/dev`、`/proc`、`/sys` 和共享 LVM 锁目录。systemd 提前启动终端与日志。这样救援命令不必临时从故障根盘加载；RAM 环境仍共用原内核，chroot 只是改变进程看到的根目录。
2. **自动保护额外准备稳定设备。** 在激活 LV、挂载根目录之前，建立 DM multipath 设备，让 LVM 使用它承载 PV。实验名为 `lab-path`，可选实机入口名为 `ram-rescue-path`。实机 `guard/native/runtime/boot.cpp` 核验登记盘后只激活已有 LV，不分区、不格式化，也不在线改接已经挂载的根卷。实际请求由内核从 LV 转发到稳定映射，再转发到 USB 分区；每次读写不经过用户空间 Guard。
3. **USB 路径失效。** 驱动将路径故障报告给上层，dm-multipath 对适用请求执行无路径排队。应用可能阻塞在读写/fsync 上，但不必立刻收到 EIO。排队不是把写入提前当作成功，也不是对任何类型错误都有效。
4. **后台找回原盘。** `guard/native/runtime/controller.cpp` 由内核块设备/DM 事件唤醒并每秒复核 sysfs 和 DM 状态；`admission.cpp` 串行调用迁移后的 `Recovery::verify()` 检查 USB 身份、容量、分区 UUID、PV/VG，并比较 LV 段布局。候选凭证同时记录设备号、diskseq、大小、逻辑块大小、boot ID、owner epoch 和期限。它能识别盘符变化，不能认证字节完全相同的克隆盘。
5. **切换后端并确认路径。** 持有候选设备描述符，通过 `load → 再次核验 → 单次 resume --noflush --nolockfs` 替换稳定设备的后端。内核在一次 resume 内完成必要的暂停、换表与恢复；没有单独的用户态 suspend 步骤。随后执行原生 `DM_MPATH_PROBE_PATHS`，再次确认实例、活动路径、表和原期限，才记录 ready。业务 I/O 在路径恢复后即可继续，探测不是业务写入屏障；上层 LV、挂载与尚存活进程不重建。
6. **失败分支。** 到期时主循环先记录 `expired`、停止准入、拒绝迟到结果。关闭无路径排队交给取得所有权锁后的接管者：旧 helper 或探测未结束时等待，不并行发送 `fail_if_no_path`。内核另有无路径超时作为补充退路，但二者都不能取消任意下层在途请求。错误可能继续传播到文件系统和应用，此后保留 RAM 救援，而不宣称完整恢复。

已安装的手动版只具备第 1 步的环境和人工核验/刷新流程，不具备第 2 步预置的排队层。它调用 `lvchange --refresh` 修正旧 LV 依赖，不能补救此前已经返回的 EIO。自动版的核心工作就是在错误扩散之前，预先把可等待、可切换的内核设备放到请求路径中。

## 1. 整体结构

根盘自动保护的数据路径如下。箭头表示 I/O 请求向下流动；PV 是 LVM 识别和组织存储的概念，不是额外转发请求的守护进程。实机已使用相同拓扑。独立数据分区省去其中的 LVM 层，文件系统直接挂载稳定 DM 映射。

```mermaid
flowchart TD
    App[原用户进程：读写与 fsync] --> FS[ext4：文件和 journal]
    FS --> LV[LVM LV：内核 DM linear 映射]
    LV --> Stable[稳定设备：内核 DM multipath]
    Stable --> USB[USB 分区：可能从 sda1 变为 sdb1]
    Guard[我们写的 RAM 路径管理程序] -. 核验身份和布局、更新路径 .-> Stable
    Guard -. 读取 sysfs 和磁盘元数据 .-> USB
    RAM[RAM 工具环境与独立 shell] --- Guard
```

稳定映射作为 PV 的承载设备，在 LV 激活和挂载根目录之前创建。断联时内核排队，重连后后台核验并切换它的后端，上层 LV 保持不变。

可能阻塞的核验、DM 变更和路径探测共用 `OwnedOperation`：同一 owner 最多一个未完成或未消费结果的任务，阶段完成通过 eventfd 唤醒主循环。健康时不创建 worker。线程只返回结果，主循环串行推进状态与 journal；线程和其 helper 持有同一 flock 的描述符，结果未消费或废弃清理未完成时也不提前放锁。超时不生成替代 worker，不能据此保证内核阻塞调用按期退出。

持有候选 fd 减少用户态检查期间的设备变化风险，但 DM 的后端查找仍按设备号进行，不能直接接收该 fd 或 diskseq。新实例若复用了当前 active 后端的 dev_t，当前实现拒绝自动切换，避免把未证明的内核对象绑定当成成功。

旧手动方案没有这个预置层：LV 直接依赖原 USB 分区，断联后可能先出现 EIO，再由人工调用 `rescue refresh` 更新 LV 映射。映射恢复不能撤销已发生的文件系统或应用错误。

## 2. 各部件由谁完成了什么

| 部件 | 职责 | 上游已有实现 | 本仓库新增工作与位置 |
|---|---|---|---|
| USB / SCSI 驱动 | 枚举设备、提交请求、报告路径故障 | Linux xHCI、UAS、usb-storage、SCSI 块设备驱动 | 选择 guest 模块、制造协议对照；未修改驱动 |
| Device Mapper 核心 | 提供稳定虚拟块设备、映射表及切换机制 | Linux DM；LVM2 的 dmsetup / libdevmapper | 编排建表、核验、加载和切换；没有自写块设备框架 |
| I/O 排队层 | 路径失效时暂存适用的请求，恢复后重试或超时报错 | Linux dm-multipath，BIO 模式、queue_if_no_path、内核无路径超时 | 选定单路径布局、配置等待策略、验证 USB 分区后端；共享 `guard/native/runtime/controller.cpp`，启动由 `boot.cpp` 或 Python 对照实验 `lab/guest/agent.py` 建表 |
| 恢复后的路径探测 | 读取当前活动组的活动路径并标记路径类错误 | Linux 6.16 起的 `DM_MPATH_PROBE_PATHS` | 版本能力查询、同步 ioctl 包装、期限/实例复核；当前要求 7.0 基线；`guard/native/runtime/core.cpp`、`controller.cpp`，见 [接入记录](lab/KERNEL-PROBE.md) |
| 路径管理程序 | 发现失效、寻找重连盘、决定是否接回、超时终止 | 调用 sysfs、libdevmapper、blkid、LVM 和 dmsetup | `Guard` 状态机、准入凭证、加载/提交/确认、终态与接管；`guard/native/runtime/controller.cpp` |
| 阻塞操作与所有权 | 串行执行故障期操作，保留锁引用，清理迟到结果 | C++ 线程、Linux flock、eventfd、进程 fd 继承 | `OwnedOperation`、有界 RAM journal 与状态；`guard/native/runtime/core.cpp` |
| 设备身份核验 | 避免只因新盘符或序列号相同就接入 | Linux sysfs/块设备 ioctl、util-linux blkid、LVM 元数据解析工具 | `guard/native/runtime/admission.cpp` 保留原 Python 身份链及带期限凭证、实例与布局核验，不另写 PV 元数据解析器 |
| 数据分区接入 | 登记已有单路径 DM 映射，排除原分区自动挂载冲突，运行独立实例 | 同一 DM、blkid、systemd、udev | `admission.cpp` 提供文件系统身份策略和运行时接入核验；`data_guard.py` 负责冷态登记；`guard/data.py` 配置临时 RAM 服务和合计配额；没有第二套恢复状态机 |
| LVM 卷管理 | PV/VG/LV 管理、LV 到物理范围的映射 | LVM2 用户态工具、Linux DM linear | 手动恢复的限制、确认流程、再次核验及结果检查；`Recovery.refresh()`；修正真实 lvs JSON 的 seg 键解析 |
| 文件系统 | 文件、目录、journal、fsync、错误处理 | Linux ext4/JBD2；e2fsprogs 提供 mkfs/e2fsck | 检查可读、可写和 journal 状态；未修改 ext4，也未实现或自动运行修复算法 |
| RAM 救援工具环境 | 根盘断联时仍能启动工具和诊断 | Linux tmpfs、chroot、挂载、cgroup；现成二进制与库 | 依赖打包、noswap、挂载安排、RAM 锁目录共用、资源限制与就绪检查；`ram-rescue-demo/build.py`、`src/prepare.sh`、`src/check.py` |
| 登录与终端 | F9/F10 文本入口和密码认证 | Linux VT、BusyBox login/sh、systemd | 独立 rescue 账号/密码配置、双终端、会话循环和服务配置；`install.py`、`src/supervisor.sh`、`src/session.sh`、`src/*.service` |
| 日志 | 保存故障前后内核消息和 helper 操作 | 内核消息接口、Python 运行时 | RAM 日志采集/轮转、操作记录；`src/kernel_log.py`、`src/rescue.py`；实验另有串口及 QMP 日志 |
| 安装与构建 | 生成运行包、服务与 guest 镜像 | Python、cpio/gzip、Ubuntu 已安装工具 | 我们的构建/安装/卸载/检查脚本；宿主包和实验镜像采用不同入口 |
| 可选实机启动接入 | 在根 LV 激活前建立保护拓扑，切根后交接 Guard | Ubuntu initramfs-tools、GRUB、udev、systemd | `guard/enroll.py`、`build.py`、`install.py`、`integration/` 与 `native/runtime/boot.cpp`；已安装的 Python 入口有实机证据，C++ 新包本轮仅在 QEMU 验证 |
| 故障实验室 | 模拟 USB 拔插、运行真实 guest 内核 | QEMU 设备模拟、KVM 加速、QMP 控制协议 | 最小 USB 根盘 guest、磁盘初始化、故障时序、错误盘/晚归/后台死亡场景、证据采集；`lab/build.py`、`run.py`、`auto_run.py`、`guest/` |
| 应用验收 | 判断进程和读写是否真的继续 | Python、Linux 文件 I/O 与 fsync 接口 | 同一 PID/启动时间、成功失败计数、延迟、直接块读取、文件前缀回读等检查；`guest/workload.py`、`agent.py` 与 runner |

LVM 是管理和构造卷映射的工具；运行中的每次块 I/O 由内核 DM 完成。`dmsetup` 是控制工具；`dm-multipath` 是内核实现；`multipathd` 则是另一个现成的用户态路径管理守护进程。**当前保护映射由 Guard 单独管理。** 实验不运行 multipathd；可选实机启动通过 `nompath` 排除 stock multipathd。flock 只协调本项目控制器，不能阻止其他 root 程序擅自修改 DM。[上游 multipath-tools](https://github.com/opensvc/multipath-tools)说明其用户态工具；排队机制见 [Linux 7.0 dm-mpath.c](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-mpath.c)。

## 3. 我们实际完成的工作

1. **保住救援入口。** 把必要程序、动态库、认证和日志放到 RAM 中，组织独立终端、启动服务和资源限制。tmpfs、登录认证、shell 和调度机制来自上游。
2. **把恢复操作约束到登记设备。** 实现身份链检查、重复候选拒绝、人工确认后的再次核验，限定已激活的登记线性 LV；实测发现并修正 LVM 段报告解析错误。
3. **将现成排队机制用于单 USB 根盘。** 在 PV 下预先建立稳定 DM 设备，编写路径恢复状态机和超时策略，避免依赖故障后的人工刷新。内核如何保存、重试请求仍由 dm-multipath 实现。
4. **建立可复现的证据链。** 在实际从 USB/LVM/ext4 启动的最小 guest 上做故障注入，对比未保护、预暂停和突发断联自动恢复；检查失败分支而不只检查成功分支。
5. **复用核心接入已有系统启动。** `guard/build.py` 默认打包 C++，显式 `--runtime python` 保留对照；新 QEMU 测试分别装入同一版完整运行时，避免只迁移监视器。实机入口检查内核、root 参数、旧事务、已有目标 VG 与 LV 依赖关系；systemd 在初始路径核验后发送 `READY=1`。已安装 Python 版本有首次实机短断证据；新 C++ 构建尚未部署到本机。

这是针对特定故障的系统集成、恢复策略和验证工作。目前没有自研 USB 驱动、文件系统、内核排队算法、虚拟机或密码算法；也没有自动复活已经退出的进程。仍存活的进程在 I/O 恢复后继续执行，才是现有成功样本的含义。

## 4. 哪些能替换

以下是工程替换判断，不代表替代方案已经通过本项目验证。

| 替换目标 | 可选方向 | 必须保留的约束 | 工作量判断 |
|---|---|---|---|
| Python Guard / Recovery | 在现有模块边界内用 Rust/C/Go 替换实现 | RAM 中可运行；唯一身份、布局检查、再次核验、有限等待、失败终态、可审计事件 | 可保持数据路径不变；当前不需要先重写语言 |
| 当前自写路径管理 | 评估 multipathd，或基于它补充登记核验与策略 | 验证单 USB/分区后端、设备身份、根盘启动、RAM 依赖及超时语义；不能让两个管理者同时修改同一张表 | 中等到高；不是安装软件就能等价替换 |
| 路径切换时 subprocess 调 dmsetup（健康查询已用 libdevmapper） | 将剩余变更操作也迁移到 libdevmapper | 保持 load、最终核验、单次 resume、探测、失败接管及锁生命周期 | 中等；减少文本命令接口，不会自动改变内核排队能力 |
| 已实现内核事件加每秒复核 | 可进一步收窄事件过滤范围 | 事件只负责唤醒；接盘前仍完整核验；不要让救援依赖故障根盘上的普通服务 | 中等；改变控制响应，不替代内核 I/O 排队 |
| BusyBox 登录和 shell | 其他登录工具、shell、文本 UI；以后增加状态提示 | 工具及依赖提前在 RAM；认证可用；保留独立终端 | 较低到中等；弹窗不应成为恢复必经步骤 |
| systemd 与打包方式 | 其他 supervisor、不同 initramfs 集成和镜像制作方案 | 控制程序在故障前就绪；所需依赖不再读故障盘；稳定映射在根 LV 挂载前建立 | 当前已有可选实机 initramfs 实现，可选项已安装，真实启动验收仍未完成 |
| 日志和通知 | RAM 环形缓冲、独立盘、远程接收端 | 写日志失败不阻塞恢复；接收路径不依赖同一故障盘 | 较低到中等；当前没有远程日志或通知服务 |
| 测试工作负载 | 已加入 Ubuntu/systemd 与 Git；可继续扩展数据库和其他真实程序 | 分别测进程存活、I/O 结果、数据一致性和应用超时 | 优先扩展；现有 fsync/Git 两类负载不能代表全部用户态 |
| QEMU/KVM 实验平台 | 更换虚拟机平台，后续接入可控物理 USB 故障设备 | 能证明真正发生断联/重枚举，并保留独立观测通道和可重现实验 | 可替换，但要重做故障注入与证据采集；QEMU 单独使用 TCG 也可运行，较慢 |
| LVM 或 ext4 | 无 LVM 的文件系统直接放稳定 DM 上；其他文件系统作为实验组 | 重新定义身份与布局登记；重新验证 flush/fsync、错误行为、恢复验收 | 高；现有核验和测试明确绑定 LVM/ext4，不能直接套用 |
| dm-multipath 内核排队层 | 自研 DM target 或其他稳定块设备后端 | 请求完成语义、重试、顺序、flush/FUA、资源上限、路径身份、超时、崩溃一致性 | 最高；这是重新承担数据路径正确性，不只是重写 Guard |

对于本项目，eBPF 更适合作为后续观测手段，例如定位延迟和错误传播；当前没有使用它。它不是把已有 EIO 改掉就能自动获得正确重试语义的替换件。若研究自有拦截机制，应先明确块层接口与请求生命周期，再定义可验证的语义。

## 5. 替换时固定什么

以下边界已经拆分到共享核心，但不是已发布的稳定插件 API。替换时应保留现有合同：

- **身份模块 `admission.py`：** 候选设备 → 持有 fd 的有限期凭证/拒绝原因；不能只返回一个易变盘符，也不能声称 fd 绑定了 DM 查找。
- **策略模块 `path_guard.py`：** 路径状态、时间、核验结果 → 等待/切换/终止决策；只有主循环推进事务。
- **操作模块 `core.cpp`：** 同一 owner 至多一个在途操作；保持锁引用、完成通知、迟到结果清理和上层设备身份。
- **证据模块 `core.cpp`：** 原子记录事务阶段与有界观察；排队关闭完成和准入到期分别记录，不通过空错误记录推出文件系统无错。
- **实验模块：** 独立制造故障、观测同一进程和数据，避免恢复程序用自己的“成功”日志替代验收。

替换 Guard 时可以继续用现有恢复、事务接管与应用实验作为对照。替换内核数据路径或文件系统时，需要扩充乱序、部分写入、flush、反复失效、资源压力和断电一致性测试，不能只要求“最后读得到文件”。恢复事务见 [TRANSACTIONS.md](lab/TRANSACTIONS.md)，实验判据见 [AUTOMATIC.md](lab/AUTOMATIC.md)。

当前实盘优先处理原 USB 根盘，独立数据分区扩展先在完整 Ubuntu VM 中验收。可选入口保留普通 Ubuntu 启动退路，不改内置 SSD 的 rEFInd；具体边界见 [guard/README.md](guard/README.md) 和 [guard/DATA.md](guard/DATA.md)。安装文件本身不会让未接入稳定 DM 的运行会话获得保护。

## 6. 上游来源与未使用方案

- [Linux 7.0 dm-multipath 源码](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-mpath.c)：当前复用的排队和探测机制；采用 Ubuntu 提供的内核和模块，未修改源码。
- [multipath-tools](https://github.com/opensvc/multipath-tools)：可评估的用户态管理替代方向；当前没有使用其中的 multipathd。
- [QEMU USB emulation](https://www.qemu.org/docs/master/system/devices/usb.html)：提供 USB 设备模拟和热插拔基础；实验场景、guest 构造和验收由本仓库完成。
- BusyBox、systemd、LVM2、util-linux、e2fsprogs、Python 及其运行库：当前从 Ubuntu 环境复制或调用；不属于本仓库重新实现。
- SystemRescue、zramroot 在旧说明中作为相关方案列举；当前没有把它们集成进运行包。systemd debug-shell 是相关机制参考，本仓库运行的是自己的服务文件。

此表是实现来源说明，不是完整依赖清单或许可证清单。公开仓库只提交源码、测试和文档，生成的 guest 镜像与运行包不随 Git 上传。
