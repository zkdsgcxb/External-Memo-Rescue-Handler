# USB 根盘恢复：内核故障模型与设计边界

调研日期：2026-09-25。此文件是技术路线报告的内核证据附件，不是部署说明。

研究对象是预先建立的 `USB 分区 → dm-multipath（BIO 模式）→ LVM 线性 LV → ext4 → Ubuntu`。源码核对使用 Linux 上游 `v6.8`，提交 `e8f897f4afef0031fe618a8e94127a0934896aba`；本地只读源码来自 `lab/work/linux-kernel-org.git`。Ubuntu `6.8.0-139-generic` 含发行版补丁，不能把上游代码核对当成逐行相同的证明；关键性质应以目标内核实验再确认。本附件未修改宿主机存储，也未执行新实验。

标记含义：**源码事实**可直接由列出的实现确认；**推论**是这些实现对设计的后果；**待实验**不能用已有快速拔插结果代替。

## 1. 建议先限定可承诺的合同

软件可以争取做到：对已经进入受保护路径、由下层以可重试路径错误完成的请求，在有限等待窗口内保持上层 DM 设备和挂载不变；只有经准入验证的同一存储内容重新出现才重试。它不能承诺“任意拔电、任意硬件错误、任意时长都让原系统毫无影响”。

至少要区分四个成功条件：

1. **路径绑定正确**：新候选的设备实例、物理身份、几何和登记内容符合策略。
2. **请求继续执行**：积压请求得到正确完成，下层没有遗留无法结束的请求。
3. **上层连续存活**：原挂载、进程、日志事务仍可使用，未观察到已外泄错误或重启。
4. **数据持久性符合合同**：已承诺持久化的数据确实保留，不能仅用返回成功、挂载仍为 `rw` 或 Git 进程尚在证明。

这些是不同的验收点，DM 路径状态 `A` 只覆盖其中一小部分。

## 2. DM 保护什么、不保护什么

**源码事实**：`multipath_end_io_bio()` 只在 `blk_path_error()` 判断可能属于路径故障时处理重排队。`BLK_STS_NOTSUPP`、`NOSPC`、`TARGET`、`RESV_CONFLICT`、`MEDIUM`、`PROTECTION` 被排除；介质、保护或不支持的操作不是“换一条路径总能恢复”的问题。没有路径且允许排队时，目标保留 BIO；禁用排队后可以向上以 I/O 错误完成。内核不是无限吸收所有 EIO 的屏障。

证据：[错误分类，blk_types.h:181–207](https://github.com/torvalds/linux/blob/v6.8/include/linux/blk_types.h#L181-L207)、[BIO 完成处理，dm-mpath.c:1691–1733](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c#L1691-L1733)。

**源码事实**：标准 multipath 状态包含路径 `A/F`、失败次数、路径组和初始化信息；其中 `MPATHF_QUEUE_IO` 是内部排队/初始化标志，不能解释成“积压请求数量”。标准输出不提供完整的无路径队列深度、最老请求等待时长、历次向上失败的请求日志，也不知道 ext4 之后是否中止日志或应用是否退出。

证据：[状态输出，dm-mpath.c:1783–1891](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c#L1783-L1891)。

**推论**：正常监视可以以 DM 事件与状态为中心，但恢复准入和系统连续性验收不能缩减成“只读一个 DM 状态”。只查内存中的状态不会主动验证盘上的数据。

## 3. 下层请求未完成不是 DM 漏拦已知错误

**源码事实**：SCSI 请求超时先经过 `scsi_timeout()`；主机驱动可以要求重新计时，也可能安排 abort 和 SCSI error handling。EH 完成后才重试请求或向上结束请求。UAS 6.8 的 abort handler 明确不实现实际向设备发送 abort，清理引用后返回 `FAILED`；进一步恢复可进入 USB device reset。UAS 的明确 disconnect 路径则取消 URB、用 `DID_NO_CONNECT` 结束待处理命令，再移除 SCSI host。因此“明确拔除”与“桥接芯片在线但不回应”并不等价。

证据：[scsi_timeout，scsi_error.c:326–370](https://github.com/torvalds/linux/blob/v6.8/drivers/scsi/scsi_error.c#L326-L370)、[UAS abort/reset](https://github.com/torvalds/linux/blob/v6.8/drivers/usb/storage/uas.c#L722-L806)、[UAS disconnect](https://github.com/torvalds/linux/blob/v6.8/drivers/usb/storage/uas.c#L1199-L1223)、[SCSI EH 设计](https://www.kernel.org/doc/html/v6.8/scsi/scsi_eh.html)。

**推论**：在这段时间里设备节点可能仍在、DM 路径可能仍为 `A`，但请求没有完成。发 `fail_path` 可以阻止后续选择该路径，不等于取消已经在控制器/驱动中的命令。不能未经取消/完成协议就复制并重新提交在途写请求。

**源码事实**：上游普通 SCSI 磁盘默认 `SD_TIMEOUT` 为 30 秒，flush 使用请求队列超时的倍数；具体机器可被驱动或 sysfs 设置改变。DM 设置的 `REQ_FAILFAST_TRANSPORT` 不是通用的“所有请求立刻失败”开关，SCSI 只在相应错误分类中使用这些标志。

证据：[sd.h 超时常量](https://github.com/torvalds/linux/blob/v6.8/drivers/scsi/sd.h#L12-L29)、[flush 超时设置](https://github.com/torvalds/linux/blob/v6.8/drivers/scsi/sd.c#L1068-L1082)、[fail-fast 分类](https://github.com/torvalds/linux/blob/v6.8/drivers/scsi/scsi_error.c#L1821-L1861)。

**待实验**：既要有 QMP `device_del/device_add`，也要有“设备仍在线、请求不完成”、USB reset 恢复、reset 失败和 UAS/BOT 差异。`dm-delay` 能证明 DM 下层未完成请求的通用影响，但不能替代 UAS 固件或 USB EH 的行为验证。

## 4. 超时不是一个从物理断开开始的全局倒计时

**源码事实**：`queue_if_no_path_timeout_secs` 的默认值是 0；本项目设置为非零才启用。计时条件是有效路径数为 0 且允许排队，`fail_path()` 调用启动逻辑。它是模块级参数，影响相应模块实例中的映射，不能当作天然的每盘隔离策略。计时器到期关闭无路径排队并处理等待请求。

证据：[计时器条件，dm-mpath.c:785–819](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c#L785-L819)、[路径失败触发，1332–1360](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c#L1332-L1360)、[模块参数，2265–2266](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c#L2265-L2266)。

**推论**：当前 Guard 8 秒、内核 10 秒不能宣称为“任何请求 10 秒内必有结果”。前面还有发现/驱动恢复时间，后面可能还有被冻结的 DM core、在途请求或命令处理时间。应分别记录物理事件、驱动结束、DM 失败、Guard 开始、准入完成、交换完成和首个完成 I/O 的时间。

**关键纠正**：`fail_if_no_path` 只是停止无路径排队，**不是全局写入栅栏，也不是永久禁止晚到成功的锁存器**。仍然可用的路径可以服务 I/O，下层在途请求也可能后来成功。当前 Guard `expired` 状态禁止它继续自动改接新盘，不能等同于“超过期限后所有写入一律不再发生”。如果产品要求后一种语义，需要独立定义并实现 fence，不能借用名称替代证明。

## 5. 映射交换及管理进程死亡必须按阶段分析

**源码事实**：即使使用 `--noflush --nolockfs`，DM suspend 仍停止新请求进入目标，并等待已经进入目标的请求完成或被退回。`noflush` 不等于“强行丢下所有在途请求立即切换”。BIO multipath presuspend 会暂时关闭自身排队，把可退回请求交给 DM core。成功 suspend 后，新的请求保存在 DM core deferred 队列中。

证据：[DM 在途等待](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm.c#L2538-L2593)、[suspend 实现](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm.c#L2692-L2790)、[BIO presuspend](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-mpath.c#L1736-L1749)。

| 中断位置 | 源码推导的后果 | 需要的实验/恢复规则 |
|---|---|---|
| 健康监视时管理器退出 | 内核现有映射仍工作；后续重枚举没人接回 | 再启动/接管时从内核重建状态，不能盲信旧 JSON |
| 无路径等待时退出 | 已启动的内核 no-path timer 可结束此队列 | 已有此类测试不能覆盖下列阶段 |
| 完成 inactive table load，尚未 suspend | active table 未切换；新表留在 inactive | 接管需核验并清除或采用，不能积累残留 |
| suspend 等待在途请求时退出 | 可能被信号中断并退回，也可能仍受驱动/锁等待影响 | 测试信号、超时及内核实际 suspended 标志 |
| **suspend 已成功、尚未 resume 时退出** | **DM core 可以持续保持 suspended；multipath timer 不负责 resume core** | 必测；需要独立、驻留 RAM 的监督/接管，不可无条件 resume 未核验表 |
| resume/swap 出错或成功但用户态未记录 | 用户态布尔值和内核事实可能不同 | 状态以 active/inactive table、UUID、suspended 标志和候选身份为准 |

当前 `lab/guest/path_guard.py` 的 `self.suspended` 只在 `dmsetup suspend` 成功返回后赋值；进程重启不能依靠它恢复。`expire()` 在该布尔值为真时先 resume 再关闭排队，必须专门审查此路径，不能宣称它是严格写入隔离操作。

**可评估的改进，不是已经验证的解决方案**：内核的 resume ioctl 在存在 inactive table 时，支持内部执行 suspend、swap、resume。用一次正确设置 flags 的控制调用，可能缩短目前两个外部命令之间的用户态中断窗口；但它不提供任意错误下的自动回滚，也不消除下层在途等待。仍要进行每阶段故障注入。

证据：[dm-ioctl.c do_resume](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-ioctl.c#L1149-L1226)。

## 6. 设备实例、设备身份与内容世代是三种不同信息

**源码事实**：`diskseq` 是每次磁盘实例分配的递增序号；可经 sysfs 或针对已经打开块设备 FD 的 `BLKGETDISKSEQ` 查询。它用于区分 `/dev/sdX`、`major:minor`、sysfs 路径复用后的实例，并不是跨重连保持不变的硬盘序列号。

证据：[diskseq ABI](https://github.com/torvalds/linux/blob/v6.8/Documentation/ABI/stable/sysfs-block#L25-L34)、[FD ioctl](https://github.com/torvalds/linux/blob/v6.8/block/ioctl.c#L513-L516)、[序号分配](https://github.com/torvalds/linux/blob/v6.8/block/genhd.c#L1465-L1468)。

建议的三层核验责任：

- **实例一致性**：dev_t、diskseq、父子设备拓扑与已打开 FD 一致；解决扫描和切换之间节点发生变化的问题。
- **存储身份与几何**：设备可靠标识、容量、逻辑扇区尺寸、分区起点/长度等符合登记；USB 桥序列号可能标识桥而非 SSD。
- **数据布局与世代**：PARTUUID/PVID/VGID、支持的 LV 布局符合预期；克隆盘、旧快照回滚和离线修改可以保留全部 UUID，因此 UUID 匹配不证明数据是最新的，更不是密码学认证。

**待实验/限定**：上游 DM table-device cache 按 dev_t 与访问模式查找已有 bdev 引用。若某类后端允许同一 dev_t 的新旧实例交叠，仅在用户态核验新 diskseq 不证明旧缓存引用已替换。普通 SCSI 索引的释放受对象寿命约束，不能据此断言当前 USB 测试有已复现漏洞；需要针对实际后端验证实例寿命与引用。

证据：[dm.c find_table_device / dm_get_table_device](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm.c#L784-L815)、[dm_get_device](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm-table.c#L339-L398)。

## 7. VFS 与文件系统并非只接受 BIO 错误一种信号

**源码事实**：`del_gendisk()` 注销磁盘与分区，最终对象释放仍由引用计数决定。Linux 6.8 的设备移除还能通过 `bdev_mark_dead()` 通知 holder；直接挂载文件系统时，`fs_bdev_mark_dead()` 可触发文件系统 shutdown。ext4 的 shutdown 回调会执行强制关闭。保留 mount 不等于保留可写/健康状态。

证据：[磁盘移除](https://github.com/torvalds/linux/blob/v6.8/block/genhd.c#L623-L731)、[bdev_mark_dead](https://github.com/torvalds/linux/blob/v6.8/block/bdev.c#L1034-L1058)、[文件系统 holder 回调](https://github.com/torvalds/linux/blob/v6.8/fs/super.c#L1397-L1412)、[ext4 shutdown](https://github.com/torvalds/linux/blob/v6.8/fs/ext4/super.c#L1483-L1486)。

**对当前拓扑的限定**：DM 打开底层设备时使用的 holder_ops 为 NULL；底层物理 bdev 死亡通知不等同于顶层永久 DM bdev 被移除。设计必须保留上层稳定映射，不能通过删除并重建同名顶层 DM 设备来“恢复”。[DM 打开底层设备](https://github.com/torvalds/linux/blob/v6.8/drivers/md/dm.c#L725-L756)

**源码事实**：ext4 错误处理可能设置 shutdown、abort journal、只读或 panic，取决于错误与策略。JBD2 abort 不能仅靠路径恢复撤销，必须关闭并重新打开日志才能退出该状态。`fsync` 也会检查和返回此前记录的写回错误。

证据：[ext4_handle_error](https://github.com/torvalds/linux/blob/v6.8/fs/ext4/super.c#L690-L758)、[JBD2 abort 合同](https://github.com/torvalds/linux/blob/v6.8/fs/jbd2/journal.c#L2534-L2564)、[ext4 fsync](https://github.com/torvalds/linux/blob/v6.8/fs/ext4/fsync.c#L129-L180)。

## 8. 断电丢失已确认写入是根本边界

**源码/协议合同**：普通块写完成可能仅表示进入设备易失写缓存。`REQ_PREFLUSH` / `REQ_FUA` 用于要求将数据推进到非易失存储；它们要求下层驱动、桥和设备遵守协议。

**推论**：如果 USB 断联同时切断 SSD/桥的电源，先前普通写已被 DM 以成功完成、相关缓存页已清洁，而硬盘易失缓存丢失，则后来重试一个 flush 并不能恢复那些已不在待重试队列里的数据。即使应用尚未 fsync，内核继续使用的文件系统状态与实际介质也可能出现分歧。因此“相同 UUID、重接成功、无可见 EIO”不是任意拔电无损的证明。

证据：[Linux 易失写缓存合同](https://www.kernel.org/doc/html/v6.8/block/writeback_cache_control.html)。

需要明确区分：数据链路复位但设备持续供电；设备掉电但持久化合同得到正确执行；设备/桥虚假报告 flush 完成；NAND/FTL 持久元数据损坏。后两类不能靠 DM 排队解决。QEMU 从同一 backing file 删除并重建设备，默认不等同于模拟消费级 SSD 断电缓存丢失、FTL 恢复或 USB 桥固件异常。

## 9. 故障分类与测试职责

| 类别 | 代表场景 | 应由谁主导 | 必须验证的结果 |
|---|---|---|---|
| 短暂传输异常 | CRC/端点 stall，驱动内部重试成功 | USB/SCSI 驱动 | 不误切路径、不触发昂贵准入 |
| 同实例 reset | reset 成功但 diskseq 不变 | 驱动；管理器观察 | 无需新盘准入；请求正确结束 |
| 明确 remove/add | 原盘以新实例返回 | 路径管理器＋准入策略 | 原顶层 DM/LV/挂载保留，旧实例不再承接 |
| 名称复用/快速交叠 | 同端口、同名、旧对象仍有引用 | 实例跟踪＋内核生命周期 | 使用 diskseq/FD；不能只比较字符串 |
| 空闲断联 | 没有 I/O 触发 DM fail | uevent＋周期状态核对 | 有限成本发现，不能以无积压证明健康 |
| 在线无响应 | 节点存在、命令迟迟不完成 | 驱动 EH；管理器只升级诊断 | 测端到端时延，不能凭 stall 就重复提交写 |
| 慢但健康 | 长 flush、拥塞、高负载 | 调度/驱动 | 无进展阈值不直接授权换盘 |
| 半就绪重连 | 盘已出现、分区/udev 尚未完成 | 事件合并＋就绪门槛 | 轻量重试，避免反复元数据扫描 |
| 错盘/重复盘 | 序列号重复、克隆盘、相同 PVID | 准入策略 | 歧义拒绝；写入前核验 |
| 几何或布局变化 | 容量/扇区/分区起点/LV extents 改变 | 准入策略＋LVM | 拒绝；不可自动修正磁盘元数据 |
| 原盘内容回滚 | 同 UUID 的旧快照或离线修改 | 数据世代策略/人工恢复 | 身份匹配不等于内容仍适合原挂载 |
| 长时间缺盘 | 永不返回或迟到 | 生命周期策略 | 有界等待、明确退化；说明与 fence 的区别 |
| 反复抖动 | 第二次断联发生在核验/交换中 | 单写者控制状态机 | epoch 丢弃过时操作、有限资源、无双恢复流程 |
| 介质/保护错误 | `MEDIUM`/`PROTECTION` | 设备/文件系统 | 明确可外泄，不能假装换路径可修复 |
| 掉电丢缓存 | 已确认普通写丢失 | 设备持久性/供电设计 | QEMU 专项缓存丢失模型，随后实际硬件验证 |
| 静默错误 | 错误数据仍报告成功 | 校验/应用/文件系统 | DM 状态不宣称检测得到 |
| 管理器故障 | SIGKILL/OOM/崩溃/限额饥饿 | RAM 驻留监督及接管 | 在 load/suspend/resume 每边界注入 |
| 事件丢失/风暴 | netlink ENOBUFS、重复乱序事件 | 管理器 | 事件只是提示；定期 reconcile、有界工作 |
| 内存压力 | 脏页、排队 BIO、swap 位于失效盘 | VM/内存管理＋策略 | 队列之外的内存峰值、OOM、其他进程存活 |
| 控制器/Hub 故障 | 多设备同时丢失、host reset | 硬件拓扑＋驱动 | 多设备故障域；单盘重枚举测试不足 |
| 整机/内核失效 | host 掉电、panic、驱动锁死 | 重启/持久恢复体系 | 不属于原 userspace 连续存活保证 |

## 10. 低占用的观测和故障注入建议

健康期只保留已登记设备的实例信息、DM 状态、单个有界事件源、低频兜底；不常驻扫描全部块设备，不不断启动 CLI，也不默认常驻高频内核追踪。块统计是轻量观测，但 `inflight` 是驱动中请求数，不等于所有层的队列深度；无完成进展也可能只是慢盘，作为疑点而不是替换授权。[sysfs inflight 定义](https://github.com/torvalds/linux/blob/v6.8/Documentation/ABI/stable/sysfs-block#L37-L50)、[块统计说明](https://www.kernel.org/doc/html/v6.8/block/stat.html)

诊断/验收时临时开启有界 trace 缓冲：关联 block issue/complete、SCSI timeout/EH、USB remove/add、diskseq、DM path event、suspend 状态和用户态阶段时间。不要把实验追踪开销算成生产监视常驻成本。

可复用内核注入工具：`dm-delay` 独立延迟读、写、flush；`dm-flakey` 注入读/写错误、丢弃写入和损坏数据。它们分别验证不同合同，不能称为同一种“拔插”。[dm-delay](https://www.kernel.org/doc/html/v6.8/admin-guide/device-mapper/delay.html)、[dm-flakey](https://www.kernel.org/doc/html/v6.8/admin-guide/device-mapper/dm-flakey.html)

对 helper 的 `subprocess timeout` 不应给出硬实时保证。当前 `rescue.py` 已注明不可中断内核等待可能超出用户态 timeout；候选盘能枚举但不能读时，串行完整核验可能停在那里。监督进程不得同时成为第二个映射写者；接管需要检测前任是否退出、是否仍有控制操作在执行，并从内核事实恢复事务。

内存预算应分开报告：管理器 RSS/PSS、RAM 中救援工具与库、日志/event buffer、内核积压请求及应用脏页。限制 Guard cgroup 不能限制所有调用者提交的 I/O 相关内存。无需新写内核模块即可先完成这些观测与生命周期改进；只有经实验确认现有接口无法表达必要的实例绑定、超时或事务保证时，才提出最小内核扩展。
