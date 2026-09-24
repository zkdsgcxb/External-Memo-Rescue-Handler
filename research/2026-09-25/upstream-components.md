# 上游组件复用与职责边界调研

核查日期：2026-09-25。范围：USB SSD 单路径、重新枚举、LVM 根盘；以下源码判断以 multipath-tools **0.9.4**、LVM2 **2.03.16**、Linux **6.8** 为基线，不能直接套用未审计的新版本。宿主只读取软件和配置，没有启动管理服务或修改宿主块设备。

## 结论

成熟组件覆盖了设备发现、WWID 归组、路径检测、路径恢复、DM 表管理和 LVM 限定设备访问。**它们没有直接提供本项目要求的“接回之前完整比较 PV/VG/LV 布局，并在保护超时后永久禁止自动恢复”的等价承诺。** 不能用安装 multipathd 代替验证，也不能把 path checker 插件当成已证明安全的接入许可接口。

推荐继续复用 Linux DM 数据路径；优先评估标准 multipathd 能承担的控制面，再决定保留小型专用管理器，或给 multipathd 增加明确的接入许可和终止状态。生产环境始终只允许**一个组件**修改受保护映射。完整性核验属于用户态策略，不应把 LVM 元数据解析放进通用内核 DM target。

## 基线与证据

本机 `dpkg-query` 显示 multipath-tools `0.9.4-5ubuntu8.2`、lvm2 `2.03.16-3ubuntu3.2`、libdevmapper `2:1.02.185-3ubuntu3.2`。上游标签源码与发行版补丁仍应区分；最终部署审计需纳入 Ubuntu 补丁集。源码缓存位于忽略目录 `lab/work/research-upstream/`。

非特权 `multipath -t` 能输出配置，但报告无权读取 `/etc/multipath/conf.d`，因此只把结果作为**部分有效配置/编译默认值**，不声称已读取完整生产配置。结果为 `allow_usb_devices no`、`find_multipaths on`、`path_checker tur`、轮询 5/20 秒、`recheck_wwid no`、`flush_on_last_del no`。本机 `lvmconfig --type default` 显示 `use_devicesfile=0`；没有独立 lvmdevices 可执行链接，但 `lvm lvmdevices --help` 可用。

## 可直接复用的能力

| 能力 | 已有机制 | 本项目注意事项 |
|---|---|---|
| USB 参与发现 | `allow_usb_devices yes` | 默认跳过 USB，必须明确启用并限定登记目标 |
| 单路径建图 | `find_multipaths strict` 与 wwids 登记文件 | strict 接受已登记 WWID；不需要伪造第二条路径 |
| 已识别路径重新上线 | WWID 归组、设备事件、DM fail/reinstate/reload | WWID 是相同存储的工程标识，不是内容认证 |
| 低频检测 | polling/max_polling、异步 TUR checker | TUR 无数据传输，但仍是设备命令；成功不能证明所有 LBA 可读 |
| 事件合并 | `uid_attrs` 以 WWID 合并 uevent | 自定义身份属性可能改变 recheck_wwid 的适用条件 |
| 全部路径丢失后的等待 | `no_path_retry`、`flush_on_last_del` | retry 是检查次数，不是精确端到端秒数；最后一条设备删除时不能立即关闭排队 |

来源：[0.9.4 配置手册](https://github.com/opensvc/multipath-tools/blob/0.9.4/multipath/multipath.conf.5)、[0.9.4 WWID 选择实现](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/wwids.c)。

TUR checker 通过 SG_IO 发送 TEST UNIT READY，`dxfer_direction=SG_DXFER_NONE`。它判断的是命令的设备可达/就绪响应，不是读取并验证用户数据。checker 的异步线程有超时与取消处理，但阻塞于下层的线程可能不能立刻被取消；源码专门处理此情形。因此它补充“DM 尚未看到普通 I/O 失败”的观察，却不能提供驱动请求必在指定毫秒内结束的保证。`directio` 则确实读取首扇区，只能证明很小范围的访问，并且繁忙时可能误判。

来源：[TUR 实现](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/checkers/tur.c)、[checker 接口和阻塞说明](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/checkers.h)。

## 完整接入核验的缺口

### WWID 重查不等于失败即拒绝

`recheck_wwid yes` 适用于规定的 SCSI 身份来源。`check_path_wwid_change()` 绕过旧 sysfs 缓存，通过 VPD 0x83 获取新值，发现变化会移除路径并重加。然而 **VPD 读取失败时，0.9.4 该函数返回 false，即“没有检测到变化”**，不是返回一个必须拒绝接入的认证错误。这不能替代本项目的失败即拒绝策略。USB 桥报告的标识是否真正随 SSD 变化，需对具体硬件验证。

`ev_add_path()` 在匹配 WWID 后还检查大小；这些检查有价值，但不读取并对照本项目的 PV/VG/LV 登记内容。源码中的 `verify_paths()` 实际检查 sysfs 的设备存在性，函数名不代表完整存储身份核验。

来源：[multipathd main.c](https://github.com/opensvc/multipath-tools/blob/0.9.4/multipathd/main.c)、[structs_vec.c](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/structs_vec.c)。

### checker 插件不能直接当成接入闸门

插件 ABI 的主要职责是返回路径健康状态。源码可见：`assemble_map()` 生成路径表时写入路径 dev_t，不编码独立的“未通过身份许可”状态；`ev_add_path()` 执行 `domap()` 后才调用 `sync_map_state()`；后者向内核发 fail/reinstate 消息。Linux `alloc_pgpath()` 的初始路径状态为 active。

**工程推论：** 只让一个自定义 checker 对错误盘返回 DOWN，不能据此证明 create/add/reload 的全过程从未把它暴露给已有排队请求。此处是源码导出的审计要求，尚未通过故障实验测出实际误写。需要独立 admission 状态，未许可候选不能进入可运行 DM 表，并审计 create、adopt、reload、reinstate、reconfigure、守护进程重启等所有入口。也不应采用“先允许，随后另一个 Guard 检查并撤销”的架构。

来源：[表生成](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/dmparser.c)、[映射构建](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/configure.c)、[内核初始路径状态](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c)。

### 终止策略也不是现成等价项

标准 multipathd 面向存储路径自动恢复。`no_path_retry` 限制等待，不能仅凭这个配置宣称“超时后即使正确盘晚归也永不自动恢复”。如果项目坚持错误已上报后不再自动放行写入，需要独立且跨管理器重启可恢复的终止状态，覆盖所有路径重加入口。`queue_without_daemon no` 是正常停止时关闭排队的策略，也不是 SIGKILL、卡死、表暂停中死亡的完整保证。

## 标准拓扑与 LVM

0.9.4 的发现代码和 udev 监听都筛选 `DEVTYPE=disk`，而当前实验用的是“USB 分区 → multipath”。标准路线应单独验证：

```text
USB 整盘 → multipath 稳定整盘 → kpartx 分区映射 → PV → LV → ext4
```

kpartx 已负责分区表到 DM 分区映射的构造，不必自己重写 GPT 解析/分区创建。该拓扑不要求重新格式化现有数据，但改变真实根盘启动依赖，必须在独立镜像中验证后准备 initramfs 集成与回退；不能在线套用实验建盘命令。

来源：[发现](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/discovery.c)、[事件过滤](https://github.com/opensvc/multipath-tools/blob/0.9.4/libmultipath/uevent.c)、[kpartx](https://github.com/opensvc/multipath-tools/blob/0.9.4/kpartx/kpartx.8)。

LVM devices file 使用设备 ID（包括 `mpath_uuid`）和 PVID，能减少命令对无关设备的访问；devname 回退可能扩大扫描。`--devices` 可直接限定一次命令。它属于 **LVM 扫描、识别和激活策略**，不会在每个已激活 LV 的 I/O 之前重新核验下层，因此不能代替 multipath 接回之前的核验。源码的 `device_ids_validate()` 还能更新记录，不能把它误当成不可变登记断言。整盘复制可以同时复制 PVID/VG UUID/文件系统 UUID，标识链依然不是密码学认证。

来源：[2.03.16 lvmdevices 手册](https://github.com/lvmteam/lvm2/blob/v2_03_16/man/lvmdevices.8_pregen)、[device_id.c 流程](https://github.com/lvmteam/lvm2/blob/v2_03_16/lib/device/device_id.c)。

启动时还要阻止普通 LVM/udev 抢先激活裸 USB PV。Ubuntu 文档明确要求配置 LVM 过滤并更新 initramfs；实际选用 devices file、过滤规则或显式设备列表应按发行版验证，不能同时保留互相矛盾的发现策略。[Ubuntu LVM over multipath](https://ubuntu.com/server/docs/explanation/intro-to/multipath/)

## 性能和 RAM 驻留

成熟 C 守护进程不自动等于更小或更安静。0.9.4 主程序尝试 `mlockall(MCL_CURRENT|MCL_FUTURE)`，并尝试设置 **SCHED_RR 优先级 99**。调度设置失败会记录警告继续运行。它还有 udev、checker、DM 事件等线程，TUR 实现会使用 checker 线程。迁移应测整个依赖集合的 PSS/RSS/VmLck、CPU、线程、唤醒次数和故障期间增量，不能只比较主可执行文件大小。

本项目已有 CPUQuota 建议不能原封不动套到 RT 线程：Linux cgroup v2 文档区分普通调度与实时调度，`cpu.max` 不提供同等的 SCHED_RR 带宽约束。部署候选应明确选择允许 RT 还是拒绝 RT 并测试普通调度响应，核验实际每线程调度策略。RAM 驻留还包括配置、插件、日志、WWID/绑定文件及 udev 依赖，不能只把 daemon 二进制放 tmpfs。

来源：[multipathd 调度、锁内存实现](https://github.com/opensvc/multipath-tools/blob/0.9.4/multipathd/main.c)、[Linux 6.8 cgroup v2](https://github.com/torvalds/linux/blob/v6.8/Documentation/admin-guide/cgroup-v2.rst)、[Ubuntu 配置文档](https://ubuntu.com/server/docs/explanation/multipath/configuring-multipath/)。

## 推荐决策门槛

1. 先以未修改的 multipathd 建立独立对照，验证单 USB 盘、稳定 WWID、标准整盘拓扑、UAS/BOT 重枚举和有限排队。
2. 若要保留本项目严格接入语义，证明身份许可在任何候选可承接 I/O 之前发生；这是选型硬门槛，优先用户态扩展，不先改内核。
3. 保留一个映射管理者。登记策略可成为该管理器的模块或受控助手；观测与救援不再争夺映射控制权。
4. 只在证明现有内核缺少所需的原子操作/可观测状态/请求终止语义时提出最小内核补丁；不要把驱动取消问题或 LVM 解析塞进 DM。
5. 若 multipathd 的完整依赖和扩展维护代价大于专用单盘管理器，不必为“复用更多代码”强行迁移；继续用上游 libdevmapper、libudev、libblkid/LVM 工具，保持自身策略小而有测试，同样符合软件工程原则。

标准 daemon 的独立实验结果由 `multipathd_probe.py` 生成，原始日志存放 `lab/work/upstream-study/`。该实验只在 RAM 根环境挂载 LVM 数据卷，不构成完整 Ubuntu 根盘迁移的验证。

## 独立 QEMU 实测结果

本节是本次实际运行，不是从源码推断。完整可审计摘要见 [multipathd-results.json](multipathd-results.json)，包括原始报告 SHA256、各次实际 runner/guest/initramfs 哈希、配置和 QEMU 命令。运行器是 [multipathd_probe.py](multipathd_probe.py)。三个结果都使用 QEMU/KVM、双 vCPU、1 GiB guest RAM、Linux 6.8.0-139-generic；仅启用一个 stock multipathd 管理整盘映射，无 Guard，根目录在 RAM，故障盘承载 kpartx/LVM/ext4 数据卷。

| 情形 | 主 daemon 调度 | 三次断联后的成功/失败 fsync 次数 | 最长写等待 | 完整前缀回读 |
|---|---|---|---|---|
| 标准权限 | SCHED_RR，99 | 231 / 0 | 0.512 秒 | 233 条匹配 |
| 移除 CAP_SYS_NICE | SCHED_OTHER，0 | 230 / 0 | 0.534 秒 | 231 条匹配 |
| 移除 CAP_SYS_NICE，换成相同呈现身份的空白盘，仅一次断联 | SCHED_OTHER，0 | 142 / 0 | 0.380 秒 | 143 条不匹配 |

写入线程在快照和最终回读之间仍继续运行，所以快照次数与最终检查条数略有差别。原盘两组每次 QMP 确认删除后立即请求重接，QMP 完成重接约 0.115–0.118 秒；这不是物理 USB 重枚举耗时上限。读取通过 `posix_fadvise(DONTNEED)` 后普通 read 进行，没有使用 O_DIRECT，也不是断电持久性验证。

原盘两组的健康阶段粗粒度 10 秒测量均约为单核 **0.10%**：使用 `/proc/PID/stat` 的进程聚合 CPU ticks，包含 daemon 线程，排除 udevd、Python 负载与内核其他工作。该短样本不能比较 100 ms 峰值，也不能直接与不同负载下的 Guard 数字作性能胜负判断。两组 daemon 均观测到 7 线程、VmRSS **27,748 kB**、VmLck **420,196 kB**。VmLck 仅为内核报告的锁定虚拟映射量，不是额外占用的物理 RAM，不推断其具体成因。移除 CAP_SYS_NICE 后 daemon 正常运行与恢复，证明该小实验无需 RT 调度；未施加 CPUQuota，因此不是配额下恢复时限测试。调度值采样的是主 PID，后续应补逐线程采样。

第三组刻意破坏了 multipath 的设备身份唯一假设：独立创建同容量的空白 raw 镜像，但 QEMU 报告相同 USB serial 和 SCSI WWID。此候选没有原来的分区/PV/文件系统内容。stock multipathd 接入后，DM 路径显示 active，已有上层挂载继续写入，应用当时没有报错；最终前缀数据校验失败。

为避免只凭软件日志认定误接入，本次从宿主只读检查独立 `wrong.raw`：观测到 **46 个完整的 4 KiB 工作负载记录，编号 97–142**，位于偏移 153,620,480 到 153,804,800；文件实际分配 1,318,912 字节。摘要记录每个完整记录的偏移与 SHA256。因此已直接证明**写请求到达了不含原数据的候选盘**。QMP 的 query-nodes 请求计数字段在本配置返回零，但最高写偏移从 0 增至 153,808,896；主要证据是直接文件内容。独立只读复核确认原盘有 0–96 共 97 条，双方另一组记录对应位置均为零。原始样本未保存初始整盘哈希或初始分配量字段，初始空白依据构造代码与 QMP 初始状态，不虚报为完整预检测量。

这不是证明 multipathd 有通用身份漏洞，而是证明**标准 WWID 归组不能满足“即便桥/设备呈现重复标识也必须对照原 PV/布局”的项目策略**。UUID 和序列号的简单组合仍不能抵抗整盘克隆；这组实验模拟身份重复/错误替换，不把它解释为攻击认证测试。它使接回前的内容登记核验从理论顾虑变成了可复现需求。

复现命令：

```bash
python3 research/2026-09-25/multipathd_probe.py
python3 research/2026-09-25/multipathd_probe.py --deny-rt
python3 research/2026-09-25/multipathd_probe.py --deny-rt --wrong
```

打包调试失败也保留在证据摘要中：BusyBox modprobe 无法读取压缩模块、ldd 依赖闭包漏掉 pthread_cancel 需要动态加载的 libgcc_s、默认 udev 属性白名单不接受 QEMU 标识、scsi_id 普通文本与导出 ID_SERIAL 的空格标准化不同。修正这些实验打包/配置问题后才得到上表结果；不将初始化失败算成热拔插能力失败，也不将 RAM 打包成功扩大为完整根启动集成已完成。
