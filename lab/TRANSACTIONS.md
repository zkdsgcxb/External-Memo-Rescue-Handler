# 当前内核 Guard 的恢复事务

当前事务实现位于 `guard/native/runtime/`，仅由 C++ 控制器执行，以 Linux `7.0.0-34-generic` / multipath target ≥ `1.15.0` 为基线。它沿用[技术路线](../research/2026-09-25/TECHNICAL-ROUTE.md)的合同；旧 Python 控制器已从当前源码移除，历史实验从固定 Git 提交复现。C++ 迁移与本次清退仅在 QEMU 验证，未替换既有实机包，也没有旧内核兼容分支。

## 职责

| 部件 | 负责 | 不代表 |
|---|---|---|
| Linux USB/SCSI、DM-multipath | 驱动请求、路径错误处理、无路径排队、原生 probe、映射切换 | 所有错误都会被拦住，或下层请求必定可取消 |
| libdevmapper | 常驻状态查询、active/inactive 表与暂停信息 | 磁盘内容身份认证 |
| LVM、blkid | 读取登记设备的 PV/VG/LV 与分区元数据 | 克隆盘不可冒充 |
| `admission.cpp` | 唯一候选、只读核验、持有 fd、带 diskseq/期限/epoch 的凭证与最终复核 | 将 fd 直接传给 DM 绑定内核对象 |
| `controller.cpp` | 唯一 owner、有限准入、提交事务、原生探测确认、终态策略 | 整个文件系统和应用已经无错 |
| `core.cpp` / `OwnedOperation` | 串行执行可能阻塞的核验、控制命令与探测；eventfd 完成通知、迟到结果清理 | 强制取消内核请求或保证线程按期退出 |
| `core.cpp` / `Owner`、`Journal` | flock/epoch、有界 RAM 记录、表摘要、分维度观察 | 掉电持久化、排斥其他 root 程序擅改 DM |
| systemd / 最小 guest 的 BusyBox shell | owner 退出后的 RAM 接管启动；继承 cgroup 预算 | 任意 D 态线程都能及时结束 |
| VM agent / host runner | 独立心跳、应用与文件系统验收、故障注入 | 生产 Guard 的第二个映射管理者 |

旧的显式 `suspend` → 用户态返回 → `resume` 流程已删除。没有追加另一个并行恢复器，也没有用 eBPF 重复内核的排队工作。stock multipathd 保留为研究对照；[选型审计](../research/2026-09-25/MULTIPATH-ADMISSION-DECISION.md)说明本轮为何不维护它的准入分叉。

## 正常与恢复路径

健康时仍用内核事件等待、合并事件与 1 秒兜底；libdevmapper 查询、sysfs 实例与完成计数读取均不主动读介质，不创建探测线程或子进程。无完成进展只是一条观察，不能授权换盘。失败后的候选查询按 100、200、400、800 ms 退避；单个恢复事务的阶段完成由 eventfd 唤醒，不为每个成功阶段额外等待退避间隔。身份读取、最终核验、修改映射及原生 probe 共用唯一的在途工作单元，主循环负责期限与状态记录。

```text
ready → waiting → verifying → load_intent → loaded
                                      ↓
                    commit_intent → committed → probe_intent → probing
                                                                ↓
                                                           confirming → ready

超时 → expired；控制事务不确定 → owner 退出 → takeover → interrupted / blocked
```

准入先确认节点、分区及唯一候选；再核验登记 USB/分区/PV/VG 身份、LV 布局、容量和逻辑块大小。打开的只读 fd 与 sysfs、BLKGETDISKSEQ、BLKGETSIZE64、BLKSSZGET 交叉核对，凭证包含 boot ID、owner epoch、登记摘要、diskseq、分区起点和期限。加载 inactive 后再次检查布局，真正提交前再次检查实例与期限。不会扫描无关磁盘。

只调用一次 `dmsetup --noudevsync resume --noflush --nolockfs lab-path`；Linux `do_resume()` 在此调用内执行必要的 suspend/swap/resume。这缩小了用户态死亡窗口，**不是任意错误下都自动回滚的原子事务**。[Linux 7.0 实现](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-ioctl.c#L1089)

新实例若复用了当前 active 路径的 dev_t，则保守拒绝自动切换：DM 表设备缓存按 dev_t/mode 查找，用户态持有 fd 并不能把 diskseq 传给 DM。此限制降低自动恢复范围，避免把未证明的绑定当成保证。[内核设备查找](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm.c#L749)

提交后最多一个 `DM_MPATH_PROBE_PATHS` 调用。调用完成还要核对 token、当前实例、active 路径、表摘要和原期限；返回零不能单独决定 ready。`ready` / `path_restored` 仅表示这份路径合同通过，应用连续性由独立负载验收。

## 死亡接管与证据

`/run/path-owner.lock` 的 flock 在进程间互斥，文件不删除。工作线程和它启动的 DM、blkid、LVM helper 持有同一 open-file-description 的 fd；owner 结束不会提前释放它们的锁引用。未消费结果也保留锁，直到主循环接收或清理完成。拿不到锁的接管者记录 `waiting_for_owner`，每秒尝试一次取得锁，不修改事务或映射，不启动替代核验。

`/run/path-transaction.json` 在副作用前后原子替换，保存 owner/boot/map 身份、阶段、原期限、候选凭证和 active/inactive 摘要。摘要只归一化内核会改变的无路径排队标志及单组选择状态；保留几何、后端和选择器参数，原始表也保存。此记录位于 RAM，跨进程存活、不跨重启；单个记录限 64 KiB。

接管者校验登记摘要和当前表，只清除已登记的 inactive，必要时再恢复已知 active 的暂停；随后关闭无路径排队、终止自动准入。**不会把上一个 owner 尚未提交的 inactive 候选晋升为 active**。未知 active/inactive 保持 blocked，等待诊断。load/resume 的命令异常可能已经产生副作用，因此进入接管，不根据命令非零退出擅自重试。

事件采用两个各不超过 64 KiB 的轮转文件；状态与 journal 各单独有界。状态分别保存身份、传输进展、控制阶段、上层错误历史。上层完整错误源尚未接入，明确为 `incomplete`，不通过空日志推出无错。

Ubuntu 使用 `RootDirectory` 进入 RAM 工具环境，启动和 `ExecStopPost` 都直接执行其中的 `guard-runtime`；实际路径由对应生产或实验 unit 指定。最小 guest 由已经运行的 BusyBox shell 等待 owner 后 exec 接管；健康时没有额外 Python 监督守护进程。二者均限制 Guard 及后代为 CPU 20% / 20 ms、cgroup memory.max 128 MiB、memory.swap.max 0。RAM 工具目录继续为 256 MiB 上限的 `tmpfs,noswap`。这些是不同记账范围，不相加；它们也不是 OOM 下绝对存活承诺。详见[资源口径](RESOURCE-MEASUREMENT.md)。

超时首先在 RAM 写入 `expired`，停止新的自动准入并拒绝迟到结果。关闭无路径排队交给拿到锁后的接管者；记录分别标注 `deferred_to_takeover` 和 `completed_by_takeover`，不能把准入截止时间当成排队已关闭。独立内核无路径计时仍有效，但它不约束所有下层在途请求。helper/probe 在不可中断等待时可以超过期限；不生成替代 worker，也不撤销已提交路径或任意在途写入。

完整 C++ 事务矩阵、实际 systemd 死亡接管和挂载结果见 [迁移验收报告](../research/2026-10-02/CPP-VM-VALIDATION.md)；本次去除旧运行时后的检查见 [清退记录](../research/2026-10-02/PYTHON-RUNTIME-RETIREMENT.md)。其中受控 NBD 阻塞只验证指定调度与解锁情形，不能计为任意永久下层阻塞均可恢复。

## 复现

先构建当前 C++ ELF，并准备匹配内核的最小 guest 基础产物。`--base-build` 指向已有实验构建目录；当前原生事务入口会替换运行时并核对实际 ELF，不将基础镜像内的旧 Python 代码当作当前控制器。完整 Ubuntu 还需要种子镜像。前置资料见 [QEMU 实验室](README.md) 和 [详细验收方法](../research/2026-10-02/CPP-VM-VALIDATION.md)。

```bash
python3 guard/native/build_runtime.py --output lab/work/cpp-runtime
python3 -m unittest discover -s lab/tests
python3 lab/cpp_transaction_probe.py matrix --base-build lab/work/route-refactor-v4
python3 lab/cpp_transaction_probe.py kill --stage after_commit --guest ubuntu --base-build lab/work/route-refactor-v4
```

较早 Python 的随机死亡、资源压力、Git 工作负载与冷读审计仍可通过 [历史入口](README.md#历史实验复现) 复现，例如 `python3 lab/historical.py lab/transaction_probe.py random-kill --build-dir lab/work/route-refactor-v4`；这些结果属于固定历史版本。

自动负载结果分别记录 `continuity_verified` 与 `expected_failure_verified`，`passed` 只是所选验收通过。阶段死亡矩阵预期得到终态，不能计成应用恢复成功。NBD 门控保持实际请求不返回直至 host 明确释放；它模拟下层等待，不等价于所有 UAS/EH 硬件行为。

工作负载在数据与目录 fsync 均返回后才写 RAM ACK；host 保存的报告是独立确认源。cold audit 另起内核和页缓存、通过新 qcow2 overlay 重放 ext4 日志并读取 ACK 前缀，保留原 raw 证据。它证明虚拟机冷读可见性，不证明物理桥接器掉电耐久。普通在线 prefix audit 不替代此项。

固定 hook 只夹住用户态可见阶段；随机 kill 也只是有限可复现样本，不代表穷尽内核 syscall 中的调度点。早期 Python 的验收与门槛保留在[历史重构记录](../research/2026-09-25/REFACTOR-RESULTS.md)，不将历史测试数量沿用为当前工作树的结果。
