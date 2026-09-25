# multipath-tools 0.15 准入扩展选型

核查日期：2026-09-25。对应 [技术路线阶段 C](TECHNICAL-ROUTE.md#13-实施顺序与退出条件)。本次选择继续收敛 **Linux 7.0 + 单一 Guard 管理者**；不增加常驻 multipathd，不维护两套运行路线。保留 stock 0.15 为独立研究对照。

这不是判断 multipathd 无法扩展。源码确实存在可集中拦截的底层入口，但要满足项目的“放行前准入、超时终止、实例不串用、管理者死亡可接管”合同，需要新增策略状态及其生命周期，并接入多条路径。仅增加一个 checker 或 `ev_add_path()` 钩子不能完成这个合同。本轮不引入未经完整验证的上游分叉；按技术路线的退出条件，优先完成已有 Guard 的事务收敛。

宿主没有安装新版 multipath-tools，没有修改真实根盘。新实验只使用 Linux `7.0.0-34-generic` 与私有构建的 multipath-tools `0.15.0`，没有旧内核适配分支。

## 1. 固定证据与判断范围

- 固定源码提交：`5a60a67d9f48ddff0d63b6e5d04c3a22764a0670`；提交归档 SHA256 为 `c0424c48fc04c82e12b5c21e359e22a58441a8fe27ad73e3255a78a5403508ae`。本轮逐一确认审计的七个 C 文件与该归档一致，文件哈希、函数行号及直接放行调用清单见 [源码审计摘要](multipath-admission-audit.json)。完整构建来源见 [既有构建记录](new-version-build.json)。以下行号均对应此固定提交。
- **本轮实测：** stock daemon 在无路径超时、应用已经收到错误之后，仍自动接回晚归原盘；DM 实际读恢复，但原 ext4/应用没有恢复。见第 4 节。
- **本轮源码结论：** checker 状态与独立准入许可不是一回事；表装入/恢复与后续 `fail_path` 有先后次序。没有将源码竞态推论冒充“新实验已测出错盘写入”。
- **历史实测：** 0.15 在 Linux 6.8 上接入相同呈现 WWID 的空白替代盘，并写入 46 条业务记录；该证据仍按原版本引用，未声称本轮在 7.0 重跑。[原记录](new-version-multipath.md)

## 2. create/add/reload/reinstate 的入口覆盖

| 放行或恢复来源 | 固定源码位置 | 必须覆盖的行为 |
|---|---|---|
| 首次 create、启动扫描和重新配置 | `libmultipath/configure.c:845 domap()`、`:1037 coalesce_paths()`；`libmultipath/devmapper.c:540 dm_addmap_create()` | 在第一次可运行表包含路径之前，核验表中全部候选；不能只拦 uevent |
| uevent 新增或重新枚举 | `multipathd/main.c:1131 uev_add_path()`、`:1301 ev_add_path()` | `ev_add_path()` 既会 CREATE，也会 RELOAD；它调用 `adopt_paths()` 收集同 WWID 路径，不只修改本次事件里的一个设备 |
| DM 事件、resize、reload、重加失败重试 | `main.c:713 update_map()`；`configure.c:939/946`；`devmapper.c:578 dm_addmap_reload()` | 在 reload 后 resume 前保持已验证实例与期限；重新构造参数后不能沿用旧许可 |
| 正常 checker 恢复路径 | `main.c:2598 update_path_state()` → `:2058 reinstate_path()` → `devmapper.c:1155 dm_reinstate_path()` | 健康检查返回 UP 并不证明 PV/布局获准；恢复状态和已终止状态必须分开 |
| 与内核同步路径状态 | `libmultipath/structs_vec.c:768 sync_map_state()`，直接调用 `dm_reinstate_path()` | 不能只在 daemon 的 `reinstate_path()` 包装函数里核验；库函数是另一入口 |
| 运维 CLI 的 reinstate | `multipathd/cli_handlers.c:1100 cli_reinstate()`，直接调用 `dm_reinstate_path()` | 此处主动启用 checker，并先把路径置为 UNCHECKED；不能依赖“checker 稍后还会检查”来维持准入 |
| standalone `multipath` | `multipath/main.c:168 get_dm_mpvec()` → `sync_map_state(mpp, true)` | 同机 CLI 和 initramfs 里的程序也要服从同一许可与唯一 owner 合同，不能只替换 daemon 二进制 |
| operator resume 与 table 残留 | `cli_handlers.c:1068 cli_resume()`；`devmapper.c:402 dm_simplecmd()` | resume 可能提交已有 inactive 表；单纯在新表生成处检查不能覆盖异常接管时的旧表 |

前三类最终收口于 `devmapper.c:455 dm_addmap()`；三个 reinstate 直接调用点最终收口于 `dm_reinstate_path()`。因此不需要在每个高层函数重复一份身份解析。可是这两个底层函数目前没有完整的异步准入事务上下文，`dm_reinstate_path()` 甚至只接收 map 名和路径 `dev_t`；引入它们必须同时设计状态的生成、失效、传递与重新发现。

另有 `devmapper.c:1585 dm_reassign_table()` 直接执行 TABLE/RELOAD/RESUME。它用于把**上层非 multipath target** 的依赖从裸设备改向 multipath，并不是另一个向 multipath 添加路径的入口；源码还明确排除修改 `TGT_MPATH` 参数。仍需在根盘集成时禁止未规划的拓扑接管，不能把搜索结果全部计成“额外准入漏洞”。

固定源文件：[devmapper.c](https://github.com/opensvc/multipath-tools/blob/5a60a67d9f48ddff0d63b6e5d04c3a22764a0670/libmultipath/devmapper.c)、[configure.c](https://github.com/opensvc/multipath-tools/blob/5a60a67d9f48ddff0d63b6e5d04c3a22764a0670/libmultipath/configure.c)、[main.c](https://github.com/opensvc/multipath-tools/blob/5a60a67d9f48ddff0d63b6e5d04c3a22764a0670/multipathd/main.c)、[CLI](https://github.com/opensvc/multipath-tools/blob/5a60a67d9f48ddff0d63b6e5d04c3a22764a0670/multipathd/cli_handlers.c)。

## 3. 为什么 checker 插件或一个同步脚本不够

`libmultipath/dmparser.c:47 assemble_map()` 把路径组中的 `dev_t` 写入 DM 表，没有输出“这条路径尚未得到身份许可”的独立标记。`ev_add_path()` 在第 1396 行执行 `domap()`，第 1434 行才执行 `sync_map_state()`；`dm_addmap_reload()` 自身在第 599 行执行 resume。Linux 7.0 的 `alloc_pgpath()` 又将新路径初始化为 active。因此，把未许可路径加入表之后再根据 DOWN 状态 `fail_path`，不能证明已有排队业务 I/O 从未接触这条路径。[表生成](https://github.com/opensvc/multipath-tools/blob/5a60a67d9f48ddff0d63b6e5d04c3a22764a0670/libmultipath/dmparser.c#L47)、[内核初始状态](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-mpath.c#L150)

此外还有四个必须处理的约束：

1. **准入读取不能堵住整个 owner。** `uev_add_path()` 在持有 `vecs->lock` 的范围内调用 `ev_add_path()`；把可能卡在下层的 LVM/blkid 同步读取直接塞进去，会把事件处理、CLI 和其他需要该锁的工作一起阻塞。应在锁外执行有界任务，再持锁核对世代并提交。超时不代表下层不可中断 I/O 已被取消，不能不断生成替代 worker。
2. **许可必须绑定实例和事务。** 仅缓存 WWID、节点名或 dev_t 会在重枚举后沿用过期许可。需要至少包含登记版本、map UUID、owner epoch、设备实例、容量/块大小和期限，并在路径移除、重初始化、配置替换和 owner 接管时失效。当前所审计的 C/H 中没有 `BLKGETDISKSEQ`/`diskseq` 引用；这只说明本项目这套凭证不能直接照搬现成字段，不是对所有实例管理机制的穷尽否定。
3. **“未许可”不能只是 DOWN。** 未许可候选不能出现在将被启用的表中，也不能通过任一 reinstate/CLI/resume 入口重新放行。已映射但状态未知的路径要先进入明确接管流程，不能通过重新启动 daemon 自动继承许可。
4. **终止状态不是 no_path_retry。** `main.c:2185 retry_count_tick()` 到期只关闭排队；`structs_vec.c:702 leave_recovery_mode()` 会再次开启排队。跨进程重启的终止记录、显式重新登记/解除方式、所有放行入口的拒绝规则仍需新增。它也不能被称为能撤销所有下层在途写入的全局写栅栏。

`verify_paths()` 只核对 sysfs 可见性；`check_path_wwid_change()` 的 VPD 查询失败仍返回 false，表示未发现变化，不能用作严格核验成功。复用这些函数有价值，但不能扩大它们的保证。[状态实现](https://github.com/opensvc/multipath-tools/blob/5a60a67d9f48ddff0d63b6e5d04c3a22764a0670/libmultipath/structs_vec.c)

## 4. Linux 7.0 + 0.15 的晚归终止反例

新增运行器 [multipath_terminal_probe.py](multipath_terminal_probe.py) 固定当前版本组合，复用既有实验的打包和追加/fsync 工作负载。QEMU/KVM 使用 2 vCPU、1 GiB RAM、UAS、独立新建的 1 GiB raw 文件，没有网络、共享目录或宿主块设备。根目录在 RAM；故障数据盘拓扑为 `整盘 multipath → kpartx 分区 → LVM → ext4`。全程只有 stock multipathd 管理映射。

配置为 `no_path_retry 8`、`polling_interval 1`、`max_polling_interval 4`、`flush_on_last_del never`、strict 登记 WWID、`recheck_wwid yes`。daemon 主线程实际为 `SCHED_OTHER/0`，`RLIMIT_RTPRIO=0`。没有应用 cgroup CPU 配额，不把这次运行当成资源比较。

| 阶段 | 实际观测 |
|---|---|
| 移除前 | 应用已写入；稳定 multipath 第一个 4 KiB 的 O_DIRECT 读成功 |
| 移除后 | daemon 从进入 recovery 到 Disable queueing 为 **8.076 秒**；应用收到 EIO，随后 EROFS |
| 文件系统 | 内核报告 journal aborted、`Remounting filesystem read-only` |
| 晚归 | QMP 确认删除到完成重接约 **15.401 秒**；daemon 自动 reload 原 map 并重新开启 queueing |
| 晚归后 | 相同 DM 设备的 4 KiB O_DIRECT 读取再次成功、哈希与之前一致；原应用成功计数仍为 4，继续收到只读错误 |

实验退出码 0 表示**预期反例确实出现**，不是应用连续性通过，也不是严格终止合同通过。读取只覆盖首个 4 KiB，不证明整盘内容健康、身份可信或文件系统恢复；没有执行自动 fsck/remount。

摘要、原始报告/串口/QMP/console 哈希和关键日志见 [multipath-terminal-result.json](multipath-terminal-result.json)。原始完整记录位于 `lab/work/admission-study/20260925-151213/`，按仓库惯例不发布虚拟磁盘。已有固定构建产物时的复现命令为：

```bash
python3 research/2026-09-25/multipath_terminal_probe.py
```

本例确认两件不同的事：stock multipathd 的晚归行为符合其自动路径恢复职责，但不符合本项目“错误已上报后停止自动接入”的额外合同；下层块访问恢复也不能撤销已经发生的 ext4 journal 错误。它没有测试 daemon 死亡、重启接管或完整 Ubuntu 根盘迁移。

## 5. 最小正确扩展的设计边界与本轮决策

若以后重启路线 C，最小可接受设计应是**一个 owner 内的一套策略**：

- 新增独立 admission 状态与不可变实例凭证；健康 checker 与许可分离。
- 锁外、数量受限的核验 worker 只返回凭证，不管理 DM 表；库层 create/reload 和 reinstate 统一检查凭证，表生成只采用获准实例。
- 把 reload/resume、CLI、启动扫描和重启接管纳入同一事务合同；inactive 表也需验证，不能仅检查当前 active 表。
- 新增有界、跨 owner 重启可识别的终止记录；先定义到期、晚结果、重新登记和管理者死亡时的行为，再允许路径复活。
- 复用 kpartx/LVM/udev、上游路径发现与 checker；核验策略不得自行再创建第二张业务映射，也不得有另一个 Guard 抢占同表。

这些变更至少涉及库的提交边界、路径生命周期/凭证、daemon 异步调度与锁、CLI/standalone 工具、终止记录/接管和根启动集成。不能用一个“调用外部脚本再返回 UP”的插件代表完成。这里没有凭空给出补丁行数或内存开销，也没有声称该设计工程上不可实现；尚无同口径性能数据支持它比收敛后的 Guard 更省资源。

**本轮采用路线 A，暂停扩展路线 C。** 当前 Guard 已有针对登记设备的布局核验、事件合并、限频核验和新内核探测；把这些合同重新移植到 0.15 后仍要补相同的事务/死亡/终止测试，还会引入新的长期补丁与整盘根启动迁移。本轮先让现有唯一 owner 满足公共 P0 合同，删除重复和无效分支；不把“不迁移”解释为公共 P0 已全部通过。

重新评估 C 的条件是获得上游可审计 admission 扩展点，或有明确需求证明通用多盘/多路径管理能抵消这套维护成本；届时必须用同一准入、错误历史、随机故障点、完整 Ubuntu 根盘和全账资源验收，不以基本拔插成功替代。当前保留的是研究证据，不是两套生产实现。
