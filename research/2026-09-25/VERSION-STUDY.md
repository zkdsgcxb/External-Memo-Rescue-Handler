# 新内核与新版 multipath 的版本对照

后续实机阶段已安装并启动相同的官方 HWE 7.0 内核，初步启动检查通过，随后按用户要求清退旧 6.8，见 [安装记录](HOST-INSTALL.md)和[首次启动记录](HOST-POSTBOOT.md)。下文描述此前隔离研究阶段的实验和时点状态。

日期：2026-09-25。本文补充 [技术路线报告](TECHNICAL-ROUTE.md)，回答“升级能否复用已有改进、避免自行扩展”，并为实机更新提供依据。本轮新增 **9 次隔离 QEMU 运行**，没有安装宿主新内核、替换宿主动态库或修改真实根盘映射。

## 1. 决策

**建议保留 Ubuntu 24.04，实机内核采用官方 HWE 更新路线；项目以新版 multipath-tools 做下一阶段标准组件基线。** 不需要把另一发行版的 glibc/systemd/udev 搬入本机，也没有证据支持现在新增内核 DM target。

新版确实包含可复用的维护工作，但升级并没有消除已经测出的核心边界：同 WWID 的错误介质仍可被标准 multipathd 接入；Linux 7.0 上，在途请求长时间不完成、DM 暂停后管理器死亡，也仍不能靠无路径计时器统一收尾。因此，后续工作重点仍是**接回前准入、唯一映射管理者、可接管的恢复事务和明确的失败语义**。

实机更新和项目部署分别验收。此次虚拟机已启动新内核并验证存储负载；它不能证明本机显示驱动、休眠、电源管理或真实 USB 桥已兼容。精确包计划与启动回退见 [HOST-UPGRADE.md](HOST-UPGRADE.md)。

## 2. 版本与来源

| 变量 | 原基线 | 本轮新增 | 验证方式 |
|---|---|---|---|
| guest 内核 | Ubuntu `6.8.0-139-generic` | Ubuntu `7.0.0-34-generic` | Ubuntu 签名 APT 索引核对包 SHA256；guest 启动记录；镜像/模块匹配 |
| multipath-tools | Noble `0.9.4-5ubuntu8.2` | 上游 `0.15.0`，提交 `5a60a67d9f48ddff0d63b6e5d04c3a22764a0670` | 标签与固定提交归档逐文件匹配；guest 二进制哈希与版本输出 |
| 基础用户空间 | Ubuntu 24.04 库和工具 | 保持原基础库 | 新版只在私有目录构建、封装进 guest |
| QEMU | `8.2.2` / KVM | 保持 | 保存完整启动命令与镜像路径 |

7.0 内核镜像和模块包版本为 `7.0.0-34.34~24.04.1`，包哈希分别为 `2bd0e1fa6fd72b00041f4523b97b730c94a9c14198a8f07231d24d49d334bd37` 和 `e81f48d95ef2ee583b342836491cc77c7fe3fbe62735b3283bc43aec5d1c7e8c`。包在 `lab/work/version-study/kernel7/` 私有解包，没有写入宿主 `/boot` 或 `/lib/modules`。

上游 multipath 构建没有修改源码；使用 Noble 的运行库及私有解包的开发依赖。为兼容旧头文件与上游警告策略，使用了上游 `WARN_ONLY=1`，并关闭不参与本次 daemon 路径的 libdmmp 构建。这是研究构建，尚不是部署软件包。具体来源、参数与哈希见 [new-version-build.json](new-version-build.json)。

## 3. 基本恢复的 2×2 对照

以下统一为 RAM 根环境中的 `USB 整盘 → multipath → kpartx → LVM → ext4 数据卷`，工作进程连续追加 4 KiB 记录并逐次 `fsync`；同盘删除、重新接入三次，最后在线回读已确认前缀。它们与下节完整 Ubuntu 根盘试验的布局不同。

| guest 内核 | 0.9.4，普通调度 | 0.15.0，普通调度 |
|---|---|---|
| 6.8.0-139 | 既有对照：三次通过，231 条前缀核验通过 | 本轮：三次通过，238 条前缀核验通过 |
| 7.0.0-34 | 本轮：三次通过，241 条前缀核验通过 | 本轮：三次通过，239 条前缀核验通过 |

四个组合均没有记录到应用写入错误。旧版普通调度采用拒绝 `CAP_SYS_NICE` 的方式；新版额外验证了保留该能力、仅继承 `RLIMIT_RTPRIO=0`，实际主线程为 `SCHED_OTHER`，三次重接及 236 条前缀核验通过。另跑的 `7.0 + 0.9.4` 默认调度对照为 `SCHED_RR/99`，三次重接及 241 条核验通过。生产 unit 的限制及全部线程调度仍须另外验证。

本轮共六次标准 daemon 试验：五次正常介质恢复、一份错误介质反例。6.8/0.9.4 的普通调度数据沿用此前已保存结果，不计入新增九次。旧、新程序使用同一工作负载与在线核验逻辑，新版将 `flush_on_last_del` 显式设为 `never` 以维持最后路径消失时继续排队的意图；并非假定所有配置字面值跨版本相同。

为了隔离版本变量，本次保留了原显式 `scsi_id` 规则和 Ubuntu DM udev 规则，未引入新版整套 systemd/udev 打包。因此，表格证明指定组合的实验可行性，不证明发行版完整包的自动发现、根盘启动或 LVM 自动激活竞争已经通过。详细结果见 [新版 multipath 研究](new-version-multipath.md)、[四次新版运行记录](new-version-results.json)、[另外五次新内核运行记录](kernel7-results.json)。

## 4. 完整 Ubuntu 根盘与 Git 负载

Linux `7.0.0-34-generic` 已从虚拟 USB/LVM/ext4 根卷运行完整 Ubuntu Server 用户空间和 systemd，继续使用现有 BIO 模式 Guard。在真实 Git 克隆过程中注入同端口、无额外等待的连续三次拔插：

| 验收项 | 观测结果 |
|---|---|
| 原系统、工作进程与 Git 进程 | boot ID、进程身份保持，服务存活检查通过 |
| 追加写入 | 统计快照中 209 次成功、0 次失败；最长单次写入约 1.445 秒 |
| 已确认数据 | 最后核验的 213 条记录、873,192 字节前缀一致；两次快照之间写入继续，故计数不同 |
| Git 结果 | 克隆结束、`git fsck` 通过、工作区干净、83,475 个跟踪文件 |
| 块读取 | 实际返回 4,096 字节，ext4 magic 为 `53ef` |
| 文件系统 | 哨兵内容相同；非只读；实际新建写入及 `fsync` 成功 |
| RAM 通道 | 保持可用，记录到的最大心跳间隔约 0.760 秒 |

Git 工作负载的源代码是 Linux **v6.8**，HEAD 为 `e8f897f4afef0031fe618a8e94127a0934896aba`；这与实际运行的 **7.0 guest 内核**是两个不同版本，不可混为一谈。

本次对原报告的块读取弱判据增加了结果审计：除了原 runner 检查，还核对真实长度、ext4 magic、哨兵内容，并检查已捕获日志中的 ext4/JBD2 错误模式。补充审计结果保存在 JSON 的 `supplemental_checks`。这次没有修改通用 runner 的旧判据，也不把“日志没有匹配”当作完整文件系统健康证明。

`--gap 0` 只表示 QMP 确认删除后不额外等待；不等于物理断联零秒、业务无停顿或真实 SSD 不掉电。1.445 秒是这次业务写入的最大观察值，不是保证上限。

## 5. 升级后仍存在的边界

| 注入 | 版本及管理方式 | 实测 | 对路线的影响 |
|---|---|---|---|
| 同容量、同 USB serial/SCSI WWID 的独立空白盘替代原盘 | 6.8 + multipathd 0.15.0 | 替代盘收到 46 条完整业务记录，编号 95–140；应用未报错，原前缀核验失败 | WWID 唯一性是标准 multipath 的信任前提；项目要求的介质/PV/布局准入仍需在放行前完成 |
| 后端限速造成在途请求停滞 | 7.0 + 现有 BIO Guard | 停滞约 12.276 秒，DM 路径仍 A、Guard 仍 ready；最长写入约 12.179 秒，解除注入后继续 | 6 秒无路径超时不限制尚在下层处理的请求；DM 路径状态不能证明完成进展 |
| 暂停映射后管理器被杀 | 7.0 + 现有 BIO Guard | 超过 6 秒内核无路径时限，9 秒观察点仍暂停；RAM 通道存活 | 暂停事务需要明确接管/退出策略；不能把无路径计时器视为全局兜底 |

错误盘的 46 条记录额外用独立 `os.pread` 按已登记偏移读取，逐条比对预期 4 KiB 内容及 SHA256；不是仅依赖扫描器计数。这是重复标识输入下的反例，并非对正常唯一 WWID 设备的通用漏洞指控。本轮没有做 `7.0 + 0.15.0` 的错误盘组合，不能把未跑组合标为实测。

后两项 `passed=true` 表示**预期的局限被复现**，不表示恢复成功。QEMU 后端限速是一种受控完成停滞，不是证明真实 UAS 驱动存在某个特定死锁。真实原事故的根因仍未由这些实验确定。

## 6. 哪些上游改进值得复用

固定 0.15.0 源码的 [NEWS](https://github.com/opensvc/multipath-tools/blob/0.15.0/NEWS.md) 记录了与本项目相关的已有工作：0.9.9 起可通过 `RLIMIT_RTPRIO` 选择普通调度；0.10/0.11 改进 checker 调度、分散检查负载；0.15 将异步执行框架扩展到更多 checker。原来的 TUR 本来就支持异步，不能把这一点全部记作 0.15 的新增收益。

这些改进使新版成为更合理的标准组件对照，但本次短样本没有测出可信的性能排名。健康 CPU 只有十秒 `/proc/PID/stat` tick 差值，部分为零不代表零开销；RSS、VmLck 也不是同一统计口径，更不能相加得到实际 RAM。各 VM 有并行运行，最大写入延迟不适合跨版本直接比较。下一次性能验收要固定负载、CPU 资源、服务限制，观察 PSS、峰值/分位 CPU、唤醒和尾延迟。

上游还明确指出 LVM2 2.03.24 及以后与旧 multipath udev 规则存在适配要求。正式升级必须验证一整套 DM/LVM/udev 配合，不能只因为私有 daemon 能启动就直接复制覆盖系统库。[固定版本兼容说明](https://github.com/opensvc/multipath-tools/blob/0.15.0/NEWS.md)

## 7. 实机更新建议与当前状态

截至本次查询，Ubuntu 官方将 Linux 7.0 列为 Ubuntu 24.04.5 的正式 HWE 路线；本机签名仓库候选是 `linux-generic-hwe-24.04=7.0.0-34.34~24.04.1`。模拟计划为新增十个包、无删除，下载约 230.8 MiB。使用这个维护渠道即可获得更新内核，不需要混装发行版。[Ubuntu HWE 文档](https://ubuntu.com/kernel/docs/reference/hwe-kernels/)

新 Linux 中确有调度和硬件支持改进，但必须区分“具有新机制”与“本机自动获得收益”。例如 `sched_ext` 需要加载相应 BPF 调度器才生效；仅升级内核不会自动改用它。此次首轮建议维持默认调度，以桌面响应、编译、待机功耗、显示、休眠及 USB 行为进行实机比较。[Linux sched_ext 文档](https://docs.kernel.org/scheduler/sched-ext.html)

主机仍运行 6.8，没有进行安装或重启。预检发现固件启动入口指向内部 NVMe ESP 的 rEFInd，而 `/boot/efi` 挂载的是 USB ESP；最终是否又链式加载其他副本尚未确认。GRUB 设置为隐藏菜单且等待零秒。进入新内核首启前必须核对实际引导链，保留旧 6.8 image/modules/initramfs，并提供能选到旧内核的菜单。完整包表、空间、回退与验收条件都已整理在 [主机升级方案](HOST-UPGRADE.md)。

## 8. 复现与后续门槛

实验构建器新增 `--module-root` 和 `--work-dir`，允许在 `lab/work/` 下为不同内核独立构建；`auto_run.py` 和 `research_probe.py` 的 `--build-dir` 选择相应镜像及记录。默认构建和 Ubuntu/Git 种子路径保持原有行为。示例使用本次已私有解包的匹配模块：

```bash
python3 lab/build.py \
  --kernel lab/work/version-study/kernel7/extracted/boot/vmlinuz-7.0.0-34-generic \
  --release 7.0.0-34-generic \
  --module-root lab/work/version-study/kernel7/extracted \
  --work-dir lab/work/version-study/kernel7/guest

python3 lab/auto_run.py \
  --build-dir lab/work/version-study/kernel7/guest \
  --guest ubuntu --workload git-clone --same-port --gap 0 --cycles 3

python3 lab/research_probe.py slow-backend \
  --build-dir lab/work/version-study/kernel7/guest
python3 lab/research_probe.py suspended-manager-death \
  --build-dir lab/work/version-study/kernel7/guest
```

模块根需要 `/lib/modules/<release>`，usr-merge 包只有 `usr/lib` 时须在私有解包根建立对应 `lib → usr/lib` 链接；再用 `depmod -b <私有根> <release>` 建立索引。完整 Ubuntu 与 Git 场景还需要先按 [Ubuntu 实验说明](../../lab/UBUNTU.md) 准备种子。新版 daemon 的独立复现命令见 [配套说明](new-version-multipath.md)。

每份运行摘要保留原报告 SHA256、runner/guest/镜像哈希及命令。此前 6.8 记录对应旧提交的源码；本轮新增的版本选择参数改变了 runner 哈希，不能据此把旧实验当作由当前源码重新运行。大镜像、构建树与原日志留在 Git 忽略的 `lab/work/`；公开仓库保留脚本、摘要和来源。

提交前复核：九份原始报告及执行时 runner 哈希匹配；新内核构建源码和输出哈希匹配；现有实验室五项、救援工具十四项单元测试通过；新增脚本及两种嵌入 guest 程序语法检查通过。提交清理仅删除 `version_probe.py` 第 49 行一个行尾空格，执行时/提交时哈希和可重建差异保存在 `new-version-results.json` 的 `post_run_runner_cleanup`，没有重写原始记录。另一审阅者独立复核了内核切换实现、错误盘实际内容和升级包计划。这些检查不扩展上述实验的故障覆盖范围。

接下来先在 0.15 基线上审计并验证所有路径放行点的最小准入方案，与原 Guard 使用同一故障矩阵比较；同时补暂停死亡、超时竞争和实例变化的事务合同。只有补丁规模、正确性与完整驻留成本都可接受，才决定替换 Guard 并删除重复管理代码。完整根盘启动集成、冷启动耐久性及真实 USB 电气故障仍是独立的后续门槛。

## 9. 实机更新后新增可用的路径探测接口

后续已完成 Guard 接入和隔离实验，见 [新内核路径探测接入](../../lab/KERNEL-PROBE.md)。以下保留接入前的源码研究时点；“尚未实测/接入”不再表示最新状态。

本节是首启、旧内核清退之后的补充源码核验，**不计入上述九次 VM 实验，也没有执行新接口的故障测试**。当前运行 `7.0.0-34-generic`；宿主 multipath-tools/kpartx 仍是 `0.9.4-5ubuntu8.2`，LVM2 为 `2.03.16-3ubuntu3.2`，libdevmapper/dmsetup 为 `1.02.185-3ubuntu3.2`。0.15 仍只用于隔离研究构建。宿主根卷仍为 USB 分区 → LVM → ext4，尚无项目 multipath 保护层；daemon 运行不表示根卷已受保护。

### 9.1 新机制及准确职责

Linux **6.16** 引入 `DM_MPATH_PROBE_PATHS`，所以从 6.8 升至 7.0 后可使用它，不能写成“7.0 首次引入”。原始用途是帮助用户态在直通 ioctl 出错后检查活动路径、推动故障转移；它不是一个完整的存储健康管理器。[引入提交](https://github.com/torvalds/linux/commit/7734fb4ad98c3fdaf0fde82978ef8638195a5285)、[原作者补丁说明](https://lists.openwall.net/linux-kernel/2025/04/29/1453)

用户态对 multipath **块设备的文件描述符**发出该 ioctl，内核对当前活动路径组中标为 Active 的路径逐条发起实际读取：从扇区 0 读取一个逻辑块；若完成状态属于 `blk_path_error`，由内核将该路径标记失效。数据不交给调用者作身份解析。它可作为“实际读探测并标坏”的现成后端，减少自写路径遍历、探测与标坏之间的协调代码。[固定 v7.0 实现](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-mpath.c#L1912)

本机 7.0 内核头文件具有该定义，匹配的实验 `dm-multipath` 模块含 `probe_active_paths` 与 `submit_bio_wait` 符号名称字符串；保留的旧 Ubuntu 6.8 实验模块未检出这些字符串。通用 `/usr/include/linux/dm-ioctl.h` 仍来自旧版用户态头文件，尚无该定义。固定 0.15 源码的 246 个 C/H 文件中未发现该 ioctl 调用，其 TUR checker 仍走 SG_IO。**安装新内核没有自动启用这个探针，既有 0.15 实验也没有验证它。** 这属于接口存在的静态支持证据，不是完整 Ubuntu 补丁审计或运行时恢复验收；来源、方法、文件哈希和限制见 [kernel-probe-interface.json](kernel-probe-interface.json)。

### 9.2 必须保留的边界

- 这是会实际读设备、可能改变 DM 路径状态的操作，不是无副作用的状态查询；不宜替代健康期每秒轻量状态检查。
- 读取使用同步 `submit_bio_wait`，逐路径执行；可能卡在下层驱动，接口自身没有独立的硬截止时间。把调用放到 worker 中也不能保证杀进程能取消内核 I/O。
- 只探测当前组中仍标为 Active 的路径，不核验新接盘身份，也不恢复已经 Failed 的路径；没有替代准入和重新接入流程。
- 返回 0 不代表所有路径都完成了成功读取：初始化/暂停准备等状态可能跳过探测，其他组仍有有效路径时也可能返回成功；某些非路径类读错误不会触发标坏。没有任何有效路径时返回 `-ENOTCONN`；还须处理分配失败、接口不支持等结果。
- 扇区 0 可读不证明全部介质、写入、flush、文件系统或应用状态正常，也不能证明过去没有向上返回过错误。

上述边界直接来自 [v7.0 探测及 ioctl 分支](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-mpath.c#L1912)。后续锁调整避免在整个探测期间持有 `work_mutex`，并合并等待同一组的并发探测，但并未给下层 I/O 加入硬超时。[后续提交](https://github.com/torvalds/linux/commit/5c977f1023156938915c57d362fddde8fad2b052)

### 9.3 对项目工作量的实际影响

| 事项 | 更新后的分工 |
|---|---|
| 排队、路径错误重试、切换后的重新提交 | 继续复用内核 DM；6.8 已有这些基础能力，不是此次新增 |
| 主动读取活动路径并标记失效 | 新增可选内核后端；先在 VM 验证，再决定是否替换自写探测部分 |
| 探测触发、限频和阻塞隔离 | 仍由唯一用户态路径管理者负责；不得与另一 owner 并发控制同表 |
| 重接介质/PV/布局准入、事务接管 | 仍是用户态职责；此接口不补齐这些缺口 |
| 已完成 I/O 的进展、错误历史及完整恢复判定 | 仍须跨块层、文件系统和应用取证 |
| 检查负载分散与新版 checker 调度 | 属于 multipath-tools 的另一次用户态升级，不能记为本次内核升级收益 |

建议将该接口纳入**故障期间、低频、最多一个未完成调用**的候选后端测试；先测拔插、在途停滞、暂停竞争、多组/初始化跳过，以及非路径错误。若它不能提供明确收益，就不增加常驻探针。现阶段不因接口存在就删除任何已验证的准入/恢复逻辑，也没有证据给出 CPU 或内存下降幅度。
