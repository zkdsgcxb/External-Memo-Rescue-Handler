# 统一后台维护

> v0.0.1-beta 用户请使用 [新的数据生命周期入口](../docs/USAGE.md)。本页保留历史 manager 与候选接口；旧 register/install 有直接启动副作用，不是新版默认安装流程。


使用一个入口登记和查看保护对象，不需要选择“根盘模式”或“数据盘模式”。程序根据已有映射与登记身份选择核验策略；已有根盘控制器由启动流程负责，数据映射出现时由 systemd 自动启动同一套 Guard。

新的保护镜像只使用完整 C++ 运行时；管理命令与 `admin/` 中的冷态登记、只读校验仍由 Python 实现。启动时校验 RAM 内的 ELF 与共享库，一小段 exec 入口交接到原生程序，没有新增常驻 shell。普通 Ubuntu 可从离线安装包独立准备数据维护环境，无需先进入保护根盘启动；当前已运行的不同版本 RAM 环境不会被热替换。缺少匹配当前内核的原生运行包时明确拒绝启动。

正常维护不弹窗，也不需要拔插后手动运行命令。没有新增定时扫盘的总管进程：`ram-rescue-manager.service` 只在启动时准备 RAM 文件，完成后为 `active (exited)`；持续监视仍由原有 Guard 实例承担。

## 当前管理范围

**管理对象是已登记、已建立的稳定 DM 映射。** 根盘映射由现有 initramfs 集成建立；数据映射由调用者或自己的启动配置建立。统一维护服务接手其后续恢复，不创建文件系统、不自动挂载，也不把正在使用的裸分区在线改接。

根盘已有 owner 会被直接识别，同一个根盘上的根 LV 与 shared LV 属于同一保护对象。登记数据映射时检查 USB 身份、分区和文件系统；首次登记后按原身份验证重连盘，不能把后来插入的另一块盘重新学习为原盘。

陌生设备保持系统原有行为。数据模式支持的文件系统、映射拓扑、无序列号设备限制以及不能覆盖的故障，沿用 [数据映射边界](DATA.md)。原根盘同一物理盘上的 EXCHANGE 等裸分区，不会因为安装统一服务就自动获得保护。EFI 继续由已有原生 udev/systemd 集成处理。

需要自动挂载、嵌套文件系统或 bind 子挂载时，可为已登记映射生成 [systemd 挂载计划](MOUNTS.md)。挂载依赖由 systemd 维护，Guard 继续只负责块设备准入和恢复；计划生成器不会直接更改当前挂载。共同断联、连续恢复及资源结果见 [QEMU 优化验收](../research/2026-10-01/OVERNIGHT-OPTIMIZATION.md)。

## 安装与使用

### 先做只读发现与预检

当前源码新增 `manager discover`。中文输出列出 USB 卷的型号、容量、挂载位置、关联映射与已知阻碍；终端内输入编号只查看**同一次快照**的详情。自动化可用 `--json`，不会提示选择。发现命令本身不写入；后续候选流程见下文。

```bash
# 工作区中仅以普通用户试读；权限不足的证据显示未知，不要 sudo 执行源码。
python3 -B guard/manage.py discover
python3 -B guard/manage.py discover --json

# 包含本次改动的包经过审阅、构建与安装后，才能使用此固定入口。
sudo /usr/bin/rescue-guard-admin manager discover
sudo /usr/bin/rescue-guard-admin manager discover --json
```

预检读取 sysfs、PID 1 挂载表、swap、udev 缓存、可信登记与安装收据，并复用 `doctor` 的当前控制器核对。只查询已登记 UUID 的 DM 表，不读取介质核验身份，不调用路径探针，也不加载模块或主动建立映射。裸分区已挂载、额外上层映射、系统盘未覆盖的兄弟分区等会单列阻碍；未知信息不会变成允许接入的结论。

输出分别显示：收据记录的准备状态、候选与运行 ELF 的冷切换差异、本次启动的控制器证据、登记 DM 表与底层依赖核对，以及未执行的恢复实验。DM 检查不验证全部 LV 布局、介质身份、文件系统或应用完整性；ELF 相同也不证明依赖已一致。快照在复读时变化则撤销当前有效结论，但两次相同不代表原子快照。历史 QEMU 报告不参与本次生效判定。

复读还覆盖根配置、登记名单与本次使用的登记内容、准备收据、当前命令运行包记录，以及管理候选使用的程序摘要和 manifest 投影。变化或变不可读时撤销相关结论；`configuration_stable` 分别标示来源。它不能捕获变化后又恢复原值的情况，也没有复核完整候选镜像/归档。

`discover --json` 是**本地未脱敏快照**，包含设备标识与挂载路径；分享时使用已有脱敏 `export`。完整的 fstab/自动挂载准入和手工启动的竞争进程仍需后续检查，首版产品目标为 Ubuntu 24.04 / x86_64 / 官方 HWE，具体组合仍须验收。本轮只读切片的测试与证据边界见 [审阅报告](../research/2026-10-05/READONLY-DISCOVERY.md)。

### 所选对象的只读候选计划

当前源码提供 `manager plan --device <对象>`，可加 `--json`。它复用发现结果及已有数据映射准入，只输出计划、阻碍和确定性摘要；**plan 本身不会保存候选或启用保护**。包含此变更的包经过审阅、构建与安装后，可由固定入口调用（本轮尚未部署）：

```bash
sudo /usr/bin/rescue-guard-admin manager plan --device /dev/mapper/rr-data-example
sudo /usr/bin/rescue-guard-admin manager plan --device /dev/mapper/rr-data-example --json
```

与 `discover` 不同，技术前置条件满足的未登记数据映射会进入完整准入，**读取选中分区介质**：成功路径执行 6–7 次 `blkid -p`，并只读持有分区、查询设备 ioctl。已有登记不重复核验接入，未建图、挂载裸分区或前置资料不完整时直接给出阻碍。产品目标已决定，但缺少候选准备前置资格或当前运行产物关联证据，仍以 `combination_unvalidated` 阻止准备；不根据 uname 或 HWE 名称自动放行，也没有 CLI 绕过。

静态组合摘要仅包含 Ubuntu 24.04 / amd64（统一 x86_64 别名）、内核与模块、管理入口/清单、运行产物和依赖清单。资格仅从固定的 `/usr/share/ram-rescue-handler/qualifications/<subject_sha256>.json` 有界可信读取，拒绝未知字段、链接、错误属主或可被组/其他用户写入的路径。root 所有只表示本地信任边界，**不是发布者签名**。资格原始内容摘要进入本机确认；设备、挂载、登记等动态输入也继续绑定本机计划，但不进入可分发组合标识。

当前采集复用固定入口的进程内代码核验，并核对当前内核/模块的 build-id、认证 Ubuntu HWE 来源、包内 runtime 和有限冷管理依赖。内核参考材料与候选资格相互独立；缺失或失配仍返回 unknown，复制资格文件不能补齐。build-id 关联采用可信 root/内核边界，不是执行内存的逐字节证明；检测到 livepatch 时本切片拒绝。具体字段见 [静态资格报告](../research/2026-10-05/STATIC-QUALIFICATION.md)，真实包与隔离 VM 的最终结果以 [真实候选验收报告](../research/2026-10-05/REAL-CANDIDATE-VALIDATION.md) 为准。ready 仍只涉及候选准备前置条件，不代表激活、根保护、恢复或完整发布验收。

身份核验命令设置 3 秒尝试超时，末段准入为 15 秒协作预算；挂载目录解析为 45 秒、systemd 查询为 4 秒的各自尝试超时。同步调用、下层阻塞和进程回收没有硬性总时限，因此只读不等于无 I/O 或保证不会卡住。程序在读取前向 stderr 提示这个边界；JSON 保持单一对象输出。

摘要绑定候选内容、拟确认效果、设备实例与关键配置输入，排除随机诊断 token 和采样时间。前后输入变化使计划失效；后续写入仍须重新核验，不能直接执行保存下来的 JSON。计划含设备身份、挂载位置及本地路径，未脱敏。完整字段、读取范围、超时审查及 fixture 验收见 [只读计划报告](../research/2026-10-05/READONLY-PLAN.md)。

### 候选准备与撤销

当前 `stage/cancel` 已完成 fixture 和一个真实管理包在隔离 Ubuntu VM 中的候选流程验收，范围为 `7.0.0-34-generic` 工程组合、已有单路径数据 DM、ext4。没有宿主部署或公开分发资格，也未验收激活和恢复；具体包、依赖与证据见 [真实候选验收报告](../research/2026-10-05/REAL-CANDIDATE-VALIDATION.md)。下面是经审阅安装后的接口说明，不要提权执行工作区源码。

```bash
# 终端内编号选择对象，展示中文计划；输入“确认”后才进入候选准备。
sudo /usr/bin/rescue-guard-admin manager stage
# 自动化须提供刚审阅的完整摘要；锁内重新生成计划，不执行外来 JSON。
sudo /usr/bin/rescue-guard-admin manager stage --device /dev/mapper/rr-data-example --expect-plan <SHA256> --json
sudo /usr/bin/rescue-guard-admin manager cancel --operation <ID> --expect-plan <SHA256> --json
```

`stage` 只适用于现有准入支持的、未登记的数据 DM。保存目标固定为 `/var/lib/ram-rescue-plans/operations/<ID>/`，与活动 registry、现有 manager 安装目录隔离。四种固定文件保存确认计划、候选登记、摘要清单和操作收据，目录 0700、文件 0600，不复制镜像或运行库。**prepared 只表示候选已准备，尚未启用；单纯重启不会生效，也不是“待重启”。** 当前运行拓扑与恢复实验仍由各自独立证据说明。

确认前不创建锁或候选目录。确认后复用管理合作锁，重新准入并比较摘要，候选写完再次重建计划；变化就拒绝或保留失败收据。同一有效计划重复准备复用原 ID；旧 cancelled 不复活。首次成功准备经过三轮 plan（展示、锁内、写后），每轮均可能读介质，继承上文无硬性总时限的限制；取消只读可信配置和候选文件，不探测介质。

取消先保存带 cleanup_pending 的收据，再核验并删除本操作的 plan.json / candidate.json，保留 manifest（若已写入）和小收据。中断后可以受限重入；链接、硬链接、额外文件、内容篡改或活动登记变化会阻止清理。无归属空目录与临时文件残留不会自动删除；`manager status` 的 `candidate_operations` 单列待审阅项，不计入受保护设备。单候选最多 1 MiB、最多 16 个未完成/未撤销操作；总操作记录最多 256 个，为每个活动操作预留最终收据空间，超限拒绝新建，不自动清退。

本流程没有建立裸分区映射、根盘启动集成、启用、停用或卸载能力。旧 `register` 仍会立即生效，是下面的既有高级接口；本轮未替它补上新的组合验收，也不能拿它证明新候选旅程已验收。具体失败边界和剩余证据见 [候选生命周期报告](../research/2026-10-05/CANDIDATE-LIFECYCLE.md)。

### 已有安装和登记命令

先按 [可信安装说明](../research/2026-10-04/INSTALLATION-AND-TRUST.md) 审阅、构建和安装离线包。特权操作只使用 root 所有的固定入口，不从用户可写工作区提权加载 Python。普通启动不要求根盘 Guard 或 `nompath`，但内核须匹配包记录、stock `multipathd.service/socket` 不得竞争，且全局 `dm_multipath.queue_if_no_path_timeout_secs` 须由管理员显式配置为至少 10 秒；默认 0 会被拒绝，工具不擅自修改全局策略。

```bash
sudo /usr/bin/rescue-guard-admin manager install
sudo /usr/bin/rescue-guard-admin manager status
sudo /usr/bin/rescue-guard-admin manager doctor
```

安装器添加统一入口、持久化 systemd/udev 配置和独立代码版本，不替换正在运行的根盘代码，不改 initrd、GRUB、rEFInd 或 fstab。后续启动自动准备维护环境。已有根盘自动出现在统一状态中，不要求重新登记：

```bash
# 可选核对：自动认出根卷对应的已有保护实例，不启动第二个 owner。
sudo /usr/bin/rescue-guard-admin manager register --device /
```

对于已经按约定建立的额外数据映射，只需登记一次：

```bash
sudo /usr/bin/rescue-guard-admin manager register \
  --device /dev/mapper/rr-data-example
```

也可指定该映射的唯一底层 USB 分区；程序查找对应的受支持映射，不要求用户声明设备用途。若选中裸分区且尚无稳定映射，会明确拒绝登记。映射名字和 UUID 的接入约定见 [DATA.md](DATA.md)。

登记后，当前存在的映射立即进入维护；后续匹配的 DM 映射出现时自动启动。**重启后自动维护，不等于自动重建数据映射**：若未配置数据映射的启动创建，状态会显示 `waiting_for_map`。精确的裸分区自动挂载排除规则仍保留，因此登记前应安排好其映射创建方式。

`manager status` 中 `ready` 表示当前控制器已就绪；`waiting_for_map` 表示登记存在但映射尚未出现；`expired/failed/interrupted/blocked` 需要检查日志，不能通过重复热插拔清空旧事务、无限延长恢复期限。过去的事件另列为 `last_state`，不将它当作当前设备仍受保护的证据。按需 `doctor` 进一步核对本次启动、实际 owner、运行副本、映射和依赖；其 `ready` 仍不保证文件系统与应用没有错误。

```bash
systemctl status ram-rescue-manager.service
journalctl -b -u 'ram-rescue-maintain@*.service'
```

命令触发的管理员认证使用本地系统认证。后台恢复本身不弹认证窗口。

## 升级管理包

先以可信流程安装已验证的新包，再调用固定入口：

```bash
sudo /usr/bin/rescue-guard-admin manager upgrade
```

升级可以保留数据登记，但要求全部本项目数据 DM 映射已按正常流程退出，相关控制器均为 `inactive`；运行中、失败或正在退出的实例都会阻止升级。命令核验已有配置、规则和收据，备份后安装独立版本并切换持久入口，执行 `daemon-reload`；不准备或替换本次 RAM 环境，不启动或停止 owner。失败尝试恢复旧配置，外部改动或回退失败保留待检查记录。

新版本仅在下一次启动生效。根盘保护镜像需另按 [保护镜像升级](README.md#升级已有保护入口) 配套更新；普通数据维护不要求进入根盘保护项。冷回退时安装保留的旧包，再执行同一 `manager upgrade` 并重启。已有完整旧版本只有在代码、依赖清单和归档逐项一致时才复用，支持 A→B→A；不修补或覆盖被改动的旧目录，不提供活跃 owner 的热升级。

## 生命周期与资源

- 精确的 DM_NAME + DM_UUID udev 规则设置 `SYSTEMD_WANTS`，只启动对应登记的维护实例；裸 USB 分区规则仅排除桌面自动挂载。
- Guard 不绑定裸设备的 systemd 生命周期。USB 消失时，Guard、稳定映射、原挂载与原进程继续存在，便于原有 I/O 排队和恢复。
- 持久登记放在 `/etc/ram-rescue-manager/devices/`，不保存 `/dev/sdX`、sysfs 实例路径或 diskseq；每次冷启动重新核验、生成当次运行配置。
- 普通启动使用独立 `tmpfs,noswap`；匹配的根盘环境通过 bind 挂载复用同一 RAM 文件，不复制第二套工具页。服务的 `RootDirectory` 使用真实 `/run/ram-rescue-manager/rootfs`，管理路径 `tools` alias 不作为 systemd 根目录。
- 冷态准备只读绑定宿主 fstab 单文件；原 inode 上编辑可见，原子替换后再次管理准入拒绝并要求正常结束数据使用后重启。udev 自动启动沿用本轮冷态基线；修改相关 fstab 规则后应重启核验。实际挂载、swap 和设备身份仍实时检查。恢复/死亡接管不重新访问宿主 fstab，不增加 `CAP_SYS_PTRACE`。
- 代码与当次登记复制到 RAM。准入及其子进程继承同一 owner fence；准备过程直接进入共用 `run_owned()`，不释放锁后另启竞争者。
- `ExecStopPost` 必须匹配本次 systemd invocation、owner 和配置摘要才能接管。未获得旧实例锁的新服务，没有权限在退出时清理旧实例。
- 继续使用 `Restart=no` 和终态记录，不增加重复恢复循环。数据实例合计 CPU 20%／20 ms、内存 256 MiB、零 swap；原根盘的配额独立保留。统一入口没有把三个 Guard 合并成一个进程。

## 撤除与失败检查

先按正常文件系统流程退出并删除额外数据映射，再撤除统一管理集成：

```bash
sudo /usr/bin/rescue-guard-admin manager uninstall
```

注册的数据映射仍存在时，撤除会拒绝，防止移除裸分区排除规则后出现二次挂载。撤除不停止现有根盘保护；登记、版本文件和安装收据保留。旧设备数据库里的属性可能保留到重新插入设备。

首次安装失败会保留 `/var/lib/ram-rescue-manager/install.json` 及已写入内容，明确记录未完成状态；不会覆盖已有安装，也没有任意部分安装的就地修补或失败后自动重装功能。核对日志和收据后再处理，不能把部分安装视为已启用。

当前普通启动的真实离线包、两次冷启动、挂载与恢复结果见 [独立数据盘验收](../research/2026-10-04/STANDALONE-DATA.md)。此前部署记录见 [统一后台维护验收](../research/2026-10-01/UNIFIED-MANAGER.md)。
